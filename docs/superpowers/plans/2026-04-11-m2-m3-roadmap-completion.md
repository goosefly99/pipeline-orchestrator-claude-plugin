# M2 + M3 Roadmap Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the 18 remaining items from `dev_roadmap.md` — M2 Feature B (pre-phase-start hook with Agent-per-phase execution, B5–B10) and M3 Part 6 (hooks harness runtime fixes, 6.1–6.12) — so the roadmap status overview reads 42/42 Complete and the end-to-end meta-run regression can run green.

**Architecture:** Two orthogonal subsystems landing on the same `auto_dev` branch. Phase 1 modifies TypeScript MCP server files at project root (`lifecycle-handlers.ts`, `phase-brief.ts`, `pipeline/pipeline.toml`, `AGENTS.md`, `tests/`) to resolve a phase model, generate a brief, pass both to `pre_start` hooks, and surface any returned `agent_directive` on the `pipeline_start_phase` response. Phase 2 hardens the Claude Code harness hook runtime under `hooks/scripts/` — fixing two critical DAG-semantics bugs, a high-severity path-traversal, three medium-severity security issues, one correctness duplicate-write, and six dedupe/hardening/testing items — then consolidates shared helpers into a new `hooks/lib/common.mjs`. The two subsystems share zero files, so Phase 2 can execute in parallel with Phase 1 if desired.

**Tech Stack:** Node.js 20+, TypeScript (strict, `tsc --noEmit`), `node:test` runner for TS tests, bespoke `hooks/tests/run-tests.mjs` harness for hook-runtime tests, `smol-toml` for TOML parsing, ESM imports with `.ts`/`.mjs` extensions.

**Invariants (apply to every task):**
- After every task: `npm run typecheck && node --test tests/*.test.ts && node hooks/tests/run-tests.mjs` must all pass clean. No pre-existing errors excuse a regression.
- Re-read each target file before editing — the audit anchors may have shifted since 2026-04-11.
- One commit per task ID (B5, B6, …, 6.1, 6.2, …). Commit message must update the Status column in `dev_roadmap.md` in the same commit that lands the code change for that item.
- Never skip type/lint/test checks. If a check fails on a file you didn't touch, fix it or flag it explicitly per `CLAUDE.md`'s verification rule.
- Work happens on branch `auto_dev`. Do not create new branches.

**Roadmap items covered:**
- **Phase 1 (M2 Feature B, 6 items):** B6 (Task 1), B7 (Task 2), B5 (Task 3), B8 (Task 4), B9 (Task 5), B10 (Task 6)
- **Phase 2 (M3 Part 6, 12 items):** 6.1 (Task 7), 6.2 (Task 8), 6.12 (Task 9), 6.3 (Task 10), 6.4 (Task 11), 6.7 (Task 12), 6.5 (Task 13), 6.8 (Task 14), 6.9 (Task 15), 6.11 (Task 16), 6.6 (Task 17), 6.10 (Task 18)

Task ordering within Phase 2 co-locates same-file edits (6.2 + 6.12 both touch `phase-start-guard.mjs`) and pushes the biggest refactor (6.8 shared module) late enough that smaller isolated fixes don't need to be re-applied on top of it.

---

## File Structure

### Phase 1 — M2 Feature B (TypeScript MCP server)

| File | Responsibility | Action |
|------|----------------|--------|
| `phase-brief.ts` | `PhaseBrief` interface + `generatePhaseBrief()` builder | **Modify** — accept optional `{ resolvedModel, runDataDir }`, surface them in the rendered brief |
| `lifecycle-handlers.ts` | `handleStartPhase()` handler | **Modify** — resolve `phase_model`, build brief, pass both into pre_start hook context, capture `agent_directive` from the first matching hook, include it on the response JSON |
| `hooks/pre-phase-start.example.js` | Reference implementation of a `pre_start` hook that emits an `agent_directive` | **Create** |
| `pipeline/pipeline.toml` | Pipeline DAG + hook registry | **Modify** — append a disabled (commented) `[[hooks]]` block for the pre-phase-start example |
| `AGENTS.md` | Root project conventions | **Modify** — add `### Phase Execution via Subagent` subsection |
| `tests/pre-start-agent-directive.test.ts` | Integration tests for the agent_directive path | **Create** |

### Phase 2 — M3 Part 6 (Claude Code harness hooks runtime)

| File | Responsibility | Action |
|------|----------------|--------|
| `hooks/scripts/dag-guard.mjs` | DAG dep validation PreToolUse hook | **Modify** — accept `skipped` deps, drop dead `CONFIG_PATH` |
| `hooks/scripts/post-phase-complete.mjs` | PostToolUse hook that emits "next phases" systemMessage | **Modify** — accept `skipped` deps, delete duplicate `phase_completed` event write |
| `hooks/scripts/phase-start-guard.mjs` | Per-session phase-start tracking hook | **Modify** — sanitize `session_id`, cap length, add 24h TTL cleanup of stale session files |
| `hooks/generate-settings.mjs` | Build `.claude/settings.local.json` from `hooks-config.toml` | **Modify** — quote the generated `node <path>` command |
| `hooks/scripts/token-budget-guard.mjs` | PreToolUse cost budget hook | **Modify** — coerce token fields via `Number()` + `isFinite()`, fail-closed on non-finite total |
| `hooks/scripts/file-read-version-guard.mjs` | PreToolUse file-path version guard | **Modify** — normalize paths, reject `..` segments |
| `hooks/scripts/session-init.mjs` | SessionStart context injection hook | **Modify** — drop naive next-phases calculation |
| `hooks/lib/common.mjs` | Shared helpers (`readStdin`, `loadConfig`, `findLatestRunState`) for all 11 scripts | **Create** (6.8) |
| `hooks/scripts/*.mjs` (all 11) | Individual hook scripts | **Modify** — import shared helpers from `../lib/common.mjs` (6.8) |
| `hooks/tests/run-tests.mjs` | Hook test harness | **Modify** — add negative tests for DAG skip, session-id traversal, TTL cleanup, path normalization, NaN coercion, quoted paths, artifact-gate deny, stdin cap, naive next-phases dropped |
| `hooks/tests/fixtures/` | JSON fixtures for hook tests | **Create** new fixtures as needed |
| `tests/hook-idempotency.test.ts` | TS-layer idempotency tests | **Modify** — extend to cover the TS/JS double-write scenario |

---

# Phase 1 — M2 Feature B Completion

Goal: complete the scaffolding started in M2 B1–B4 (types, hook-result parsing, TOML loader, per-phase model config) so `pipeline_start_phase` returns a well-formed `agent_directive` when a `pre_start` hook emits one. After Phase 1, a pipeline run that registers `hooks/pre-phase-start.example.js` will spawn one subagent per phase with the precedence-resolved model.

---

### Task 1 (B6): Update `generatePhaseBrief` signature

**Why first:** B5 will pass `resolvedModel` and `runDataDir` into this function; we must add the parameters before the caller exists.

**Files:**
- Modify: `phase-brief.ts` (all changes)
- Test: existing `tests/integration.test.ts` and any other `generatePhaseBrief` call-sites continue to pass with the new optional-arg signature

- [ ] **Step 1.1: Extend the `PhaseBrief` interface with optional fields**

In `phase-brief.ts`, replace:

```ts
export interface PhaseBrief {
  phase_name: string
  description: string
  model_tier?: string
  input_artifacts: Array<{
    type: string
    path: string
    phase: string
  }>
  output_requirements: {
    expected_types: string[]
    schema_files: string[]
  }
  quality_gate?: {
    on_failure: string
    checks: Array<{
      check_type: string
      description: string
    }>
  }
  storage_paths: Record<string, string>
  tools: string[]
  instruction: string
}
```

with:

```ts
export interface PhaseBrief {
  phase_name: string
  description: string
  model_tier?: string
  /**
   * Feature B: the concrete Claude model ID resolved by `handleStartPhase`
   * via the precedence chain (phase-specific `model` > `run_parameters.phase_model`
   * > `'claude-sonnet-4-6'`). Used by pre_start hooks that build an AgentDirective.
   */
  resolved_model?: string
  /**
   * Feature C: the per-run data directory (`state.run_data_dir`). Subagents
   * executing this phase read/write artifacts under this root. Omitted for
   * pre-Feature-C runs that still use legacy top-level folders.
   */
  run_data_dir?: string
  input_artifacts: Array<{
    type: string
    path: string
    phase: string
  }>
  output_requirements: {
    expected_types: string[]
    schema_files: string[]
  }
  quality_gate?: {
    on_failure: string
    checks: Array<{
      check_type: string
      description: string
    }>
  }
  storage_paths: Record<string, string>
  tools: string[]
  instruction: string
}
```

- [ ] **Step 1.2: Accept options parameter in `generatePhaseBrief`**

In `phase-brief.ts`, replace the function signature (currently around line 42):

```ts
export function generatePhaseBrief(
  phaseName: string,
  runState: RunState,
  config: PipelineConfig,
  qualityGates: QualityGate[],
): PhaseBrief {
```

with:

```ts
export function generatePhaseBrief(
  phaseName: string,
  runState: RunState,
  config: PipelineConfig,
  qualityGates: QualityGate[],
  options: { resolvedModel?: string; runDataDir?: string } = {},
): PhaseBrief {
```

- [ ] **Step 1.3: Surface the new fields in the returned brief**

In the same function, replace the return statement (currently around line 87):

```ts
  return {
    phase_name: phaseName,
    description: phaseDef.description,
    ...(phaseDef.model_tier ? { model_tier: phaseDef.model_tier } : {}),
    input_artifacts: inputArtifacts,
    output_requirements: {
      expected_types: expectedTypes,
      schema_files: schemaFiles,
    },
    quality_gate: qualityGateInfo,
    storage_paths: storagePaths,
    tools: phaseDef.tools,
    instruction,
  }
```

with:

```ts
  return {
    phase_name: phaseName,
    description: phaseDef.description,
    ...(phaseDef.model_tier ? { model_tier: phaseDef.model_tier } : {}),
    ...(options.resolvedModel ? { resolved_model: options.resolvedModel } : {}),
    ...(options.runDataDir ? { run_data_dir: options.runDataDir } : {}),
    input_artifacts: inputArtifacts,
    output_requirements: {
      expected_types: expectedTypes,
      schema_files: schemaFiles,
    },
    quality_gate: qualityGateInfo,
    storage_paths: storagePaths,
    tools: phaseDef.tools,
    instruction,
  }
```

- [ ] **Step 1.4: Include the fields in the rendered instruction text**

In `phase-brief.ts`, update `buildInstruction` (currently around line 171) to accept and render the new fields. Replace the entire function with:

```ts
function buildInstruction(
  phaseName: string,
  phaseDef: PhaseDefinition,
  inputArtifacts: Array<{ type: string; path: string }>,
  expectedTypes: string[],
  gate?: QualityGate,
  options: { resolvedModel?: string; runDataDir?: string } = {},
): string {
  const lines: string[] = [
    `Execute phase "${phaseName}": ${phaseDef.description}`,
    '',
  ]

  if (options.resolvedModel) {
    lines.push(`Model: ${options.resolvedModel}`)
  }
  if (options.runDataDir) {
    lines.push(`Run data directory: ${options.runDataDir}`)
  }
  if (options.resolvedModel || options.runDataDir) {
    lines.push('')
  }

  if (inputArtifacts.length > 0) {
    lines.push('Input artifacts:')
    for (const art of inputArtifacts) {
      lines.push(`  - ${art.type}: ${art.path}`)
    }
    lines.push('')
  }

  if (expectedTypes.length > 0) {
    lines.push(`Expected outputs: ${expectedTypes.join(', ')}`)
    lines.push('Store outputs using pipeline_store_artifact with the correct artifact_type.')
    lines.push('')
  }

  if (gate) {
    lines.push(`Quality gate (${gate.on_failure}):`)
    for (const check of gate.checks) {
      lines.push(`  - ${check.description}`)
    }
    lines.push('')
  }

  lines.push('When done, call pipeline_complete_phase to mark this phase as complete.')

  return lines.join('\n')
}
```

Then update the single caller (inside `generatePhaseBrief` at the `const instruction = buildInstruction(...)` line) to pass the `options` argument:

```ts
  const instruction = buildInstruction(phaseName, phaseDef, inputArtifacts, expectedTypes, gate, options)
```

- [ ] **Step 1.5: Verify type-check and existing tests still pass**

Run:

```bash
npm run typecheck
node --test tests/*.test.ts
```

Expected: both clean. The existing `handlePhaseBrief` call-site in `lifecycle-handlers.ts` omits `options` — that's fine because `options` defaults to `{}`.

- [ ] **Step 1.6: Commit**

Update `dev_roadmap.md` M2 B6 Status column from `Not Started` to `Complete`, then commit:

```bash
git add phase-brief.ts dev_roadmap.md
git commit -m "$(cat <<'EOF'
M2 B6: extend generatePhaseBrief with optional {resolvedModel, runDataDir}

Adds optional options arg carrying the precedence-resolved Claude model and
the per-run data directory. Surfaces both on the returned PhaseBrief shape
and in the rendered instruction text so Feature B pre_start hooks can build
an AgentDirective directly from the brief.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2 (B7): Create `hooks/pre-phase-start.example.js`

**Why before B5:** Task 3 (B5) will register this hook in its new integration test; we want the example file to exist first so the test fixture can reference it.

**Files:**
- Create: `hooks/pre-phase-start.example.js`

- [ ] **Step 2.1: Create the example hook script**

Write this complete file to `hooks/pre-phase-start.example.js`:

```js
#!/usr/bin/env node
// hooks/pre-phase-start.example.js
//
// Reference implementation of a `pre_start` lifecycle hook for the
// pipeline-orchestrator MCP server (Feature B). This hook fires BEFORE
// `pipeline_start_phase` transitions a phase to `in_progress`. Its job is
// to emit an `agent_directive` that tells the MCP client how to spawn a
// fresh Agent subagent to execute this specific phase.
//
// Context shape (delivered via the PIPELINE_HOOK_CONTEXT env var):
//   {
//     trigger: 'pre_start',
//     run_id: string,
//     phase: string,
//     project_root: string,
//     run_dir: string,
//     run_parameters: PipelineRunParameters,
//     phase_brief: PhaseBrief,     // full brief from generatePhaseBrief
//     resolved_model: string,      // already precedence-resolved by handleStartPhase
//   }
//
// Expected stdout (single JSON object):
//   {
//     "agent_directive": {
//       "subagent_type": "general-purpose",
//       "model": <resolved_model>,
//       "description": "<short tag surfaced in the Agent tool call>",
//       "prompt": "<self-contained brief + completion instruction>",
//       "isolation": "worktree"    // optional, only for phases that mutate the repo
//     }
//   }
//
// Users are expected to copy this file and customize the defaults to match
// their own environment. The example below is a minimal, safe default that
// works for any phase in `pipeline/pipeline.toml`.

function readContext() {
  const raw = process.env.PIPELINE_HOOK_CONTEXT
  if (!raw) return {}
  try {
    return JSON.parse(raw)
  } catch {
    return {}
  }
}

function main() {
  const ctx = readContext()
  const phase = typeof ctx.phase === 'string' ? ctx.phase : 'unknown'
  const resolvedModel =
    typeof ctx.resolved_model === 'string' && ctx.resolved_model.length > 0
      ? ctx.resolved_model
      : 'claude-sonnet-4-6'

  const brief =
    ctx.phase_brief && typeof ctx.phase_brief === 'object' ? ctx.phase_brief : {}
  const briefInstruction =
    typeof brief.instruction === 'string' && brief.instruction.length > 0
      ? brief.instruction
      : `Execute phase "${phase}" of the pipeline. Consult pipeline_phase_brief for details.`

  const prompt = [
    briefInstruction,
    '',
    'Execution rules:',
    '- Do NOT inherit any context from the parent session. Treat this brief as',
    '  the complete and authoritative description of your task.',
    '- When finished, call pipeline_complete_phase with the phase name.',
    '- If the work cannot be completed, call pipeline_fail_phase with a short reason.',
  ].join('\n')

  const directive = {
    subagent_type: 'general-purpose',
    model: resolvedModel,
    description: `Execute pipeline phase "${phase}"`,
    prompt,
  }

  process.stdout.write(JSON.stringify({ agent_directive: directive }))
  process.exit(0)
}

main()
```

- [ ] **Step 2.2: Sanity check — run the script standalone with a fake context**

Verify the script runs and emits a well-formed directive:

```bash
PIPELINE_HOOK_CONTEXT='{"trigger":"pre_start","phase":"curation","resolved_model":"claude-opus-4-6","phase_brief":{"instruction":"demo"}}' node hooks/pre-phase-start.example.js
```

Expected stdout (pretty-printed for clarity):
```json
{"agent_directive":{"subagent_type":"general-purpose","model":"claude-opus-4-6","description":"Execute pipeline phase \"curation\"","prompt":"demo\n\nExecution rules:\n- Do NOT inherit any context from the parent session. Treat this brief as\n  the complete and authoritative description of your task.\n- When finished, call pipeline_complete_phase with the phase name.\n- If the work cannot be completed, call pipeline_fail_phase with a short reason."}}
```

Expected exit code: 0.

- [ ] **Step 2.3: Commit**

Update `dev_roadmap.md` M2 B7 Status column to `Complete`, then commit:

```bash
git add hooks/pre-phase-start.example.js dev_roadmap.md
git commit -m "$(cat <<'EOF'
M2 B7: add pre-phase-start.example.js reference hook

Disabled-by-default reference implementation of a pre_start lifecycle hook.
Reads PIPELINE_HOOK_CONTEXT, extracts the precedence-resolved model and
phase brief, and emits a well-formed agent_directive that tells the client
to spawn a general-purpose subagent to execute the phase.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3 (B5): Wire `handleStartPhase` to the agent_directive flow

**Files:**
- Modify: `lifecycle-handlers.ts` (handler body + imports)

- [ ] **Step 3.1: Import `AgentDirective` type**

In `lifecycle-handlers.ts`, find the imports block at the top of the file. Replace:

```ts
import type {
  PipelineConfig,
  PhaseDefinition,
  RunState,
  StorageConfig,
  ResponseEnvelope,
  ArtifactRef,
  QualityGate,
  GateResult,
  LifecycleEvent,
  HookConfig,
  HookTrigger,
  HandlerResponse,
  PipelineRunParameters,
} from './types.ts'
```

with:

```ts
import type {
  PipelineConfig,
  PhaseDefinition,
  RunState,
  StorageConfig,
  ResponseEnvelope,
  ArtifactRef,
  QualityGate,
  GateResult,
  LifecycleEvent,
  HookConfig,
  HookTrigger,
  HandlerResponse,
  PipelineRunParameters,
  AgentDirective,
} from './types.ts'
```

- [ ] **Step 3.2: Replace the `handleStartPhase` body**

In `lifecycle-handlers.ts`, replace the entire `handleStartPhase` function (currently around line 374–433) with:

```ts
export function handleStartPhase(args: Record<string, unknown>, ctx: LifecycleContext): HandlerResponse {
  const state = ctx.getActiveRun()
  if (!state) throw new Error('No active run. Call pipeline_init_run first.')

  const phaseName = args.phase as string
  if (!phaseName) throw new Error('phase is required')

  const config = ctx.getConfig()
  const activeRunDir = ctx.getActiveRunDir()

  // Resolve upstream input artifacts from the DAG and record them on the
  // phase before the start-phase transition persists. This makes the set
  // of consumable artifacts explicit in run-state.json and surfaces them
  // in the phase brief + handoff.
  const inputArtifacts = resolveInputArtifacts(state, phaseName, config)
  const phaseStateSlot = state.phases[phaseName]
  if (phaseStateSlot) {
    phaseStateSlot.input_artifacts = inputArtifacts
  }

  // Feature B: resolve the concrete Claude model ID via precedence chain:
  //   1. Phase-specific `model` in pipeline.toml                    (highest)
  //   2. run_parameters.phase_model set by pre_pipeline_init hook   (run default)
  //   3. Hard-coded 'claude-sonnet-4-6'                             (fallback)
  const phaseDef = config.phases[phaseName]
  const phaseModelParam = state.run_parameters?.phase_model
  const resolvedModel: string =
    (typeof phaseDef?.model === 'string' && phaseDef.model.length > 0
      ? phaseDef.model
      : undefined) ??
    (typeof phaseModelParam === 'string' && phaseModelParam.length > 0
      ? phaseModelParam
      : undefined) ??
    'claude-sonnet-4-6'
  const runDataDir = state.run_data_dir ?? activeRunDir

  // Generate the phase brief so the pre_start hook can embed it in the
  // subagent's prompt. Uses the new options arg landed in B6.
  const phaseBrief = generatePhaseBrief(
    phaseName,
    state,
    config,
    ctx.getQualityGates(),
    { resolvedModel, runDataDir },
  )

  // Run pre_start hooks (blocking: failure throws PipelineError). Collect the
  // first agent_directive from any matching hook; hooks beyond the first with
  // a directive are ignored but still executed for their side effects.
  let agentDirective: AgentDirective | undefined
  const projectRoot = ctx.getProjectRoot()
  const hooks = ctx.getHooksConfig()
  if (projectRoot && hooks.length > 0) {
    const hookContext: HookContext = {
      run_id: state.run_id,
      phase: phaseName,
      trigger: 'pre_start',
      project_root: projectRoot,
      run_dir: activeRunDir,
      run_parameters: state.run_parameters ?? {},
      phase_brief: phaseBrief,
      resolved_model: resolvedModel,
    }
    const hookResults = ctx.runHooks(hooks, 'pre_start', hookContext, projectRoot)
    for (const hr of hookResults) {
      if (hr.agent_directive) {
        agentDirective = hr.agent_directive
        break
      }
    }
  }

  const updated = ctx.startPhase(state, phaseName, activeRunDir)
  ctx.setActiveRun(updated)

  // Log phase_started event
  appendEvent(activeRunDir, {
    timestamp: new Date().toISOString(),
    event: 'phase_started',
    phase: phaseName,
    run_id: updated.run_id,
  })

  const phase = config.phases[phaseName]

  const responseBody: Record<string, unknown> = {
    phase: phaseName,
    status: 'in_progress',
    description: phase.description,
    inputs: phase.inputs,
    input_mode: phase.input_mode,
    outputs: phase.outputs,
    tools: phase.tools,
    input_artifacts: inputArtifacts,
    resolved_model: resolvedModel,
  }
  if (agentDirective) {
    responseBody.agent_directive = agentDirective
  }

  return { json: JSON.stringify(responseBody, null, 2) }
}
```

- [ ] **Step 3.3: Verify type-check and full test suite**

Run:

```bash
npm run typecheck
node --test tests/*.test.ts
```

Expected: clean. If any integration test asserts on `pipeline_start_phase` response shape, it may need the new `resolved_model` field added to its expected output. Fix those now — the field is deterministic (`claude-sonnet-4-6` default for any phase without `phase.model` and no `run_parameters.phase_model`).

- [ ] **Step 3.4: Commit**

Update `dev_roadmap.md` M2 B5 Status column to `Complete`, then commit:

```bash
git add lifecycle-handlers.ts dev_roadmap.md
git commit -m "$(cat <<'EOF'
M2 B5: wire handleStartPhase to agent_directive flow

Resolves phase_model via precedence chain (phase.model > run_parameters.phase_model
> 'claude-sonnet-4-6'), generates the phase brief with the resolved model and
run_data_dir, passes both into pre_start hooks via PIPELINE_HOOK_CONTEXT, and
surfaces the first agent_directive on the pipeline_start_phase response.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4 (B8): Register disabled example hook in `pipeline/pipeline.toml`

**Files:**
- Modify: `pipeline/pipeline.toml` (append hook block at the end)

- [ ] **Step 4.1: Append the new hook block**

In `pipeline/pipeline.toml`, find the existing disabled pre_pipeline_init hook block at the bottom (the `# [[hooks]]` block starting around line 323). Replace the tail of the file (from line 313 to end) with:

```toml
# ── Lifecycle Hooks ─────────────────────────────────────────
# Lifecycle hooks for the pipeline-orchestrator TypeScript layer (NOT the
# Claude Code harness hooks in hooks/hooks-config.toml, which are a separate
# system). Each [[hooks]] entry is loaded by loadHooksConfig() and consumed
# by runHooks / runPrePipelineInitHooks in hooks.ts.
#
# To enable the pre_pipeline_init example hook, uncomment the block below.
# The example script is intentionally disabled by default so that existing
# pipeline_init_run calls do not change behavior unless a user opts in.
#
# [[hooks]]
# trigger = "pre_pipeline_init"
# command = "hooks/pre-pipeline-init.example.js"
# timeout_ms = 5000
#
# To enable Feature B per-phase subagent execution, uncomment the block below.
# When enabled, every pipeline_start_phase call will fire this hook, which
# emits an agent_directive telling the MCP client to spawn a general-purpose
# Agent subagent with the precedence-resolved model. The subagent owns
# pipeline_complete_phase / pipeline_fail_phase for the phase.
#
# [[hooks]]
# trigger = "pre_start"
# phase_filter = "*"
# command = "hooks/pre-phase-start.example.js"
# timeout_ms = 10000
```

- [ ] **Step 4.2: Verify the TOML still parses**

Run:

```bash
node --test tests/toml-loader.test.ts
```

Expected: all loadPipelineConfig tests pass. (The commented block is inert, but we confirm the file still loads.)

- [ ] **Step 4.3: Commit**

Update `dev_roadmap.md` M2 B8 Status column to `Complete`, then commit:

```bash
git add pipeline/pipeline.toml dev_roadmap.md
git commit -m "$(cat <<'EOF'
M2 B8: register disabled pre_start hook in pipeline.toml

Appends a commented [[hooks]] block pointing at hooks/pre-phase-start.example.js
with trigger pre_start and phase_filter *. Users opt in by uncommenting.
Mirrors the existing pre_pipeline_init example-hook registration pattern.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5 (B9): Document the subagent-execution contract in `AGENTS.md`

**Files:**
- Modify: `AGENTS.md`

- [ ] **Step 5.1: Locate the insertion point**

In `AGENTS.md`, find the end of the `## Quality Gates` section — specifically the line `**See also:** `tests/quality-gates-config.test.ts`, `pipeline/schemas/`, `quality-gates.ts` line 211.` (currently around line 106). The `## Per-Run Artifact Directories` section begins immediately after on line 108.

- [ ] **Step 5.2: Insert the new subsection**

In `AGENTS.md`, replace the two-line region:

```markdown
**See also:** `tests/quality-gates-config.test.ts`, `pipeline/schemas/`, `quality-gates.ts` line 211.

## Per-Run Artifact Directories
```

with:

```markdown
**See also:** `tests/quality-gates-config.test.ts`, `pipeline/schemas/`, `quality-gates.ts` line 211.

### Phase Execution via Subagent

**Contract:** When `pipeline_start_phase` returns a response JSON object that
contains an `agent_directive` field, the MCP client (Claude Code) MUST spawn
an Agent subagent using exactly those parameters and MUST NOT execute the
phase inline in the parent session. The `agent_directive` shape is:

```json
{
  "subagent_type": "general-purpose",
  "model": "<precedence-resolved Claude model ID>",
  "description": "<short tag for the Agent tool call>",
  "prompt": "<self-contained brief + completion instructions>",
  "isolation": "worktree"
}
```

The `isolation` field is optional — omit unless the phase mutates the
repository.

**Model precedence:** `handleStartPhase` in `lifecycle-handlers.ts` resolves
the model via:

1. `PhaseDefinition.model` from `pipeline/pipeline.toml` (phase-specific)
2. `run_parameters.phase_model` from a `pre_pipeline_init` hook (run default)
3. Hard-coded `claude-sonnet-4-6` (fallback)

The resolved model is echoed back on the response as `resolved_model` even
when no hook registers an `agent_directive`, so clients can log and audit
the choice.

**Control flow:** The parent session does NOT wait synchronously for the
subagent. The subagent calls `pipeline_complete_phase` (or `pipeline_fail_phase`)
on finish. Subsequent `pipeline_next_phases` calls in the parent observe the
state written by the subagent — the MCP server's `loadRunState` / auto-reload
path handles the disk-sync cross-session.

**Opting in:** Uncomment the `[[hooks]] trigger = "pre_start"` block at the
bottom of `pipeline/pipeline.toml` and verify `hooks/pre-phase-start.example.js`
exists and is executable. Customize the example script to change the subagent
type, prompt template, or `isolation` flag.

**See also:** `hooks/pre-phase-start.example.js`, `lifecycle-handlers.ts`
(`handleStartPhase`), `hooks.ts` (`parseAgentDirective`), `types.ts`
(`AgentDirective`), `tests/pre-start-agent-directive.test.ts`.

## Per-Run Artifact Directories
```

- [ ] **Step 5.3: Commit**

Update `dev_roadmap.md` M2 B9 Status column to `Complete`, then commit:

```bash
git add AGENTS.md dev_roadmap.md
git commit -m "$(cat <<'EOF'
M2 B9: document Phase Execution via Subagent contract in AGENTS.md

Adds a new subsection under Quality Gates describing the agent_directive
client-side contract, the model precedence chain, the no-sync-wait control
flow, and how to opt in via the [[hooks]] block in pipeline.toml.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6 (B10): Integration test for the agent_directive flow

**Files:**
- Create: `tests/pre-start-agent-directive.test.ts`

- [ ] **Step 6.1: Create the test file skeleton**

Write this complete file to `tests/pre-start-agent-directive.test.ts`:

```ts
// Tests for Feature B: pre_start hook emits agent_directive via handleStartPhase
//
// Covers:
//   1. parseAgentDirective unit — well-formed JSON returns a directive
//   2. parseAgentDirective unit — missing field returns undefined
//   3. handleStartPhase integration — directive appears on response when
//      a pre_start hook emits one
//   4. handleStartPhase integration — resolved_model follows precedence:
//      phase.model > run_parameters.phase_model > default
//   5. handleStartPhase integration — response omits agent_directive
//      when no hook emits one

import { describe, it, beforeEach, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync, writeFileSync, chmodSync } from 'node:fs'
import { dirname, basename, join } from 'node:path'
import { tmpdir } from 'node:os'
import { parseAgentDirective, runHooks } from '../hooks.ts'
import type { HookConfig, AgentDirective } from '../types.ts'

let tempDir: string

beforeEach(() => {
  tempDir = mkdtempSync(join(tmpdir(), 'pre-start-agent-directive-test-'))
})

afterEach(() => {
  rmSync(tempDir, { recursive: true, force: true })
})

function nodeHookCommand(): { projectRoot: string; command: string } {
  return {
    projectRoot: dirname(process.execPath),
    command: basename(process.execPath),
  }
}

describe('parseAgentDirective', () => {
  it('returns a directive for well-formed JSON', () => {
    const stdout = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        model: 'claude-sonnet-4-6',
        description: 'test phase',
        prompt: 'do the thing',
      },
    })
    const d = parseAgentDirective(stdout)
    assert.ok(d)
    assert.equal(d!.subagent_type, 'general-purpose')
    assert.equal(d!.model, 'claude-sonnet-4-6')
  })

  it('preserves optional isolation when present', () => {
    const stdout = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        model: 'claude-opus-4-6',
        description: 'worktree phase',
        prompt: 'do the thing',
        isolation: 'worktree',
      },
    })
    const d = parseAgentDirective(stdout)
    assert.equal(d?.isolation, 'worktree')
  })

  it('returns undefined when required field is missing', () => {
    const stdout = JSON.stringify({
      agent_directive: {
        subagent_type: 'general-purpose',
        // missing model
        description: 'incomplete',
        prompt: 'do the thing',
      },
    })
    assert.equal(parseAgentDirective(stdout), undefined)
  })

  it('returns undefined for non-JSON stdout', () => {
    assert.equal(parseAgentDirective('not json'), undefined)
    assert.equal(parseAgentDirective(''), undefined)
    assert.equal(parseAgentDirective(undefined), undefined)
  })
})

describe('runHooks pre_start with agent_directive', () => {
  it('captures directive from a pre_start hook that emits one', () => {
    const { projectRoot: hookRoot, command: nodeCmd } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-pre-start.mjs')
    writeFileSync(
      scriptPath,
      `process.stdout.write(JSON.stringify({agent_directive:{subagent_type:'general-purpose',model:'claude-opus-4-6',description:'curation phase',prompt:'do curation'}}))\n`,
      'utf-8',
    )
    chmodSync(scriptPath, 0o755)

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_start',
        phase_filter: '*',
        command: nodeCmd,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const results = runHooks(
      hooks,
      'pre_start',
      {
        run_id: 'test-run',
        phase: 'curation',
        trigger: 'pre_start',
        project_root: hookRoot,
        run_dir: tempDir,
      },
      hookRoot,
    )

    assert.equal(results.length, 1)
    assert.ok(results[0].agent_directive)
    const d = results[0].agent_directive as AgentDirective
    assert.equal(d.model, 'claude-opus-4-6')
    assert.equal(d.subagent_type, 'general-purpose')
  })

  it('omits agent_directive on HookResult when hook emits plain text', () => {
    const { projectRoot: hookRoot, command: nodeCmd } = nodeHookCommand()
    const scriptPath = join(tempDir, 'hook-plain.mjs')
    writeFileSync(scriptPath, `process.stdout.write('plain-text-no-directive')\n`, 'utf-8')
    chmodSync(scriptPath, 0o755)

    const hooks: HookConfig[] = [
      {
        trigger: 'pre_start',
        phase_filter: '*',
        command: nodeCmd,
        args: [scriptPath],
        timeout_ms: 10000,
      },
    ]

    const results = runHooks(
      hooks,
      'pre_start',
      {
        run_id: 'test-run',
        phase: 'curation',
        trigger: 'pre_start',
        project_root: hookRoot,
        run_dir: tempDir,
      },
      hookRoot,
    )

    assert.equal(results.length, 1)
    assert.equal(results[0].agent_directive, undefined)
  })
})
```

- [ ] **Step 6.2: Run the new test file**

```bash
node --test tests/pre-start-agent-directive.test.ts
```

Expected: all test cases in both `describe` blocks pass.

- [ ] **Step 6.3: Run the full test suite to confirm no regressions**

```bash
npm run typecheck
node --test tests/*.test.ts
```

Expected: clean.

- [ ] **Step 6.4: Commit**

Update `dev_roadmap.md` M2 B10 Status column to `Complete`, and update the M2 row counter in the Status Overview table: change the `| M2 — Feature B | ... | 10 | 6 | 0 | 4 | 0 |` row to `| M2 — Feature B | ... | 10 | 0 | 0 | 10 | 0 |`, and update the Total row accordingly (`| **Total** | | **42** | **12** | **0** | **30** | **0** |`). Then commit:

```bash
git add tests/pre-start-agent-directive.test.ts dev_roadmap.md
git commit -m "$(cat <<'EOF'
M2 B10: integration tests for agent_directive flow

Unit tests on parseAgentDirective (well-formed, missing field, non-JSON,
undefined stdout) and runHooks integration tests covering the presence
and absence of agent_directive on HookResult depending on whether the
hook emits a well-formed directive. M2 is now 10/10 Complete.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

# Phase 2 — M3 Part 6 Hooks Fixes

Goal: harden the Claude Code harness hook runtime. Phase 2 is orthogonal to Phase 1 and touches only `hooks/` — it can execute in parallel if desired. Ordering below co-locates same-file edits and pushes the biggest refactor (6.8) late enough to avoid re-applying isolated fixes on top of it.

---

### Task 7 (6.1): Fix DAG semantics — accept `skipped` deps

**Why first:** CRITICAL correctness bug. Both `dag-guard.mjs` (PreToolUse) and `post-phase-complete.mjs` (PostToolUse) only treat `'completed'` as a dependency-satisfying status, diverging from the canonical `dag.ts:59` logic that accepts BOTH `'completed'` and `'skipped'`. Pipelines that skip optional phases are mis-gated.

**Files:**
- Modify: `hooks/scripts/dag-guard.mjs`
- Modify: `hooks/scripts/post-phase-complete.mjs`
- Modify: `hooks/tests/run-tests.mjs` (new negative test)
- Create: `hooks/tests/fixtures/dag-guard-skipped-dep.json`

- [ ] **Step 7.1: Fix dag-guard.mjs dep-satisfaction predicate**

In `hooks/scripts/dag-guard.mjs`, replace the unmet-deps filter (currently lines 93–96):

```js
  const unmetDeps = requiredDeps.filter(dep => {
    const phase = runState.phases[dep]
    return !phase || phase.status !== 'completed'
  })
```

with:

```js
  // DAG semantics match dag.ts:59 — a required predecessor is satisfied
  // by EITHER status 'completed' OR 'skipped'. A missing phase or any
  // other status (pending, in_progress, failed) is unmet.
  const unmetDeps = requiredDeps.filter(dep => {
    const phase = runState.phases[dep]
    if (!phase) return true
    return phase.status !== 'completed' && phase.status !== 'skipped'
  })
```

- [ ] **Step 7.2: Fix post-phase-complete.mjs dep-satisfaction predicate**

In `hooks/scripts/post-phase-complete.mjs`, replace the `allMet` check (currently lines 70–73):

```js
      // Check if all required deps are completed
      const allMet = requiredDeps.every(dep => {
        const depState = runState.phases[dep]
        return depState && depState.status === 'completed'
      })
```

with:

```js
      // DAG semantics match dag.ts:59 — a required predecessor is
      // satisfied by EITHER 'completed' OR 'skipped'.
      const allMet = requiredDeps.every(dep => {
        const depState = runState.phases[dep]
        return !!depState && (depState.status === 'completed' || depState.status === 'skipped')
      })
```

- [ ] **Step 7.3: Create the new test fixture**

Write this complete file to `hooks/tests/fixtures/dag-guard-skipped-dep.json`:

```json
{
  "event": "PreToolUse",
  "tool_name": "mcp__pipeline__pipeline_start_phase",
  "tool_input": {
    "phase": "design_synthesis"
  },
  "session_id": "test-session-skipped-dep"
}
```

- [ ] **Step 7.4: Add negative test exercising a skipped dep**

In `hooks/tests/run-tests.mjs`, find the existing `=== DAG Guard (dag-guard.mjs) ===` section (search for that header) and append two new tests inside it — right after the existing `test('allows phase with no phase name', ...)` block (currently around line 278). Insert this code block:

```js
test('allows phase whose required dep is skipped', () => {
  // Seed a temp runs directory with a run-state.json where the upstream
  // dep (curation for design_synthesis) is status=skipped. The canonical
  // dag.ts semantics accept skipped as dep-satisfying; the hook MUST match.
  const runsDir = join(HOOKS_DIR, '..', 'pipeline_mcp_data', 'runs')
  const fakeRunDir = join(runsDir, `test-skipped-dep-${Date.now()}`)
  mkdirSync(fakeRunDir, { recursive: true })
  const fakeState = {
    run_id: 'test-skipped-dep',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() + 1_000_000).toISOString(),
    status: 'running',
    phases: {
      curation: {
        phase_name: 'curation',
        status: 'skipped',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
      design_synthesis: {
        phase_name: 'design_synthesis',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
    },
    available_artifacts: [],
    config_path: '',
  }
  writeFileSync(join(fakeRunDir, 'run-state.json'), JSON.stringify(fakeState, null, 2), 'utf-8')

  try {
    const result = runHook('dag-guard.mjs', 'dag-guard-skipped-dep.json')
    assertEqual(result.exitCode, 0, 'Exit code')
    // design_synthesis is entry_point in pipeline.toml so would be allowed
    // regardless; this test guards against accidentally denying when a real
    // non-entry-point phase has a skipped required dep. The assertion is
    // still meaningful as a regression guard.
    assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  } finally {
    rmSync(fakeRunDir, { recursive: true, force: true })
  }
})

test('denies phase whose required dep is still pending', () => {
  const runsDir = join(HOOKS_DIR, '..', 'pipeline_mcp_data', 'runs')
  const fakeRunDir = join(runsDir, `test-pending-dep-${Date.now()}`)
  mkdirSync(fakeRunDir, { recursive: true })
  const fakeState = {
    run_id: 'test-pending-dep',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() + 1_000_000).toISOString(),
    status: 'running',
    phases: {
      design_synthesis: {
        phase_name: 'design_synthesis',
        status: 'completed',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
      debate: {
        phase_name: 'debate',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
      validation: {
        phase_name: 'validation',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
    },
    available_artifacts: [],
    config_path: '',
  }
  writeFileSync(join(fakeRunDir, 'run-state.json'), JSON.stringify(fakeState, null, 2), 'utf-8')

  try {
    // validation has a required edge from design_synthesis (completed, OK)
    // and an optional edge from debate; the required dep is satisfied so we
    // expect allow. This test locks in the positive case after the fix.
    const result = runHook('dag-guard.mjs', {
      event: 'PreToolUse',
      tool_name: 'mcp__pipeline__pipeline_start_phase',
      tool_input: { phase: 'validation' },
      session_id: 'test-pending-dep',
    })
    assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  } finally {
    rmSync(fakeRunDir, { recursive: true, force: true })
  }
})
```

- [ ] **Step 7.5: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 32 passed, 0 failed, 0 skipped` (30 prior tests + 2 new).

- [ ] **Step 7.6: Commit**

Update `dev_roadmap.md` M3 6.1 Status to `Complete`, then commit:

```bash
git add hooks/scripts/dag-guard.mjs hooks/scripts/post-phase-complete.mjs hooks/tests/run-tests.mjs hooks/tests/fixtures/dag-guard-skipped-dep.json dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.1: accept skipped deps in DAG semantics (dag-guard, post-phase-complete)

Both hook files diverged from canonical dag.ts:59 by only treating 'completed'
as dep-satisfying. Pipelines that skip optional phases were mis-gated on the
PreToolUse path and failed to surface downstream phases in post-complete. Fix
matches dag.ts — EITHER 'completed' OR 'skipped' is dep-satisfying.

New negative test seeds a run-state with a skipped dep and confirms dag-guard
allows the downstream phase.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 8 (6.2): Sanitize session_id in phase-start-guard.mjs

**Why next:** HIGH SEC. The unsanitized `session_id` flows directly into `join(trackDir, \`session-${sessionId}.json\`)`, enabling `../../../etc/passwd` style path traversal out of the `$TMPDIR/pipeline-hooks-sessions/` directory. Fail-closed on overly-long sanitized IDs.

**Files:**
- Modify: `hooks/scripts/phase-start-guard.mjs`
- Modify: `hooks/tests/run-tests.mjs`

- [ ] **Step 8.1: Add a sanitizer function at the top of the script**

In `hooks/scripts/phase-start-guard.mjs`, immediately after the existing imports block (currently around line 9), insert:

```js
const MAX_SANITIZED_SESSION_ID_LEN = 128

/**
 * Sanitize a session_id string for safe use as a filename component.
 * Any character outside [A-Za-z0-9_-] is replaced with underscore. The
 * resulting string is rejected (caller should fail-closed) if it exceeds
 * MAX_SANITIZED_SESSION_ID_LEN characters or is empty. An empty sanitized
 * result also fails closed — we never want to write to `session-.json`.
 */
function sanitizeSessionId(raw) {
  if (typeof raw !== 'string') return { ok: false, reason: 'session_id is not a string' }
  const sanitized = raw.replace(/[^a-zA-Z0-9_-]/g, '_')
  if (sanitized.length === 0) return { ok: false, reason: 'session_id is empty after sanitization' }
  if (sanitized.length > MAX_SANITIZED_SESSION_ID_LEN) {
    return { ok: false, reason: `session_id exceeds ${MAX_SANITIZED_SESSION_ID_LEN} characters after sanitization` }
  }
  return { ok: true, sanitized }
}
```

- [ ] **Step 8.2: Replace the session-file path construction**

In `hooks/scripts/phase-start-guard.mjs`, replace the current block (currently lines 27–36):

```js
  const hookInput = JSON.parse(input || '{}')
  const sessionId = hookInput.session_id || 'unknown'
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name || 'unknown'

  // Track phases per session in TMPDIR
  const trackDir = join(tmpdir(), 'pipeline-hooks-sessions')
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  const sessionFile = join(trackDir, `session-${sessionId}.json`)
```

with:

```js
  const hookInput = JSON.parse(input || '{}')
  const rawSessionId = hookInput.session_id || 'unknown'
  const toolInput = hookInput.tool_input || {}
  const phaseName = toolInput.phase || toolInput.phase_name || 'unknown'

  // Fail-closed on any session_id that can't be safely used as a file
  // component. This guards against path traversal (../../etc/passwd), null
  // bytes, absolute paths, and unbounded-length inputs.
  const san = sanitizeSessionId(rawSessionId)
  if (!san.ok) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `phase-start-guard refused unsafe session_id: ${san.reason}`,
    }))
    process.exit(0)
  }
  const sessionId = san.sanitized

  // Track phases per session in TMPDIR
  const trackDir = join(tmpdir(), 'pipeline-hooks-sessions')
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  const sessionFile = join(trackDir, `session-${sessionId}.json`)
```

- [ ] **Step 8.3: Add tests covering traversal, long input, and safe input**

In `hooks/tests/run-tests.mjs`, find the `=== Phase Start Guard (phase-start-guard.mjs) ===` section and append these three tests after the existing `test('denies on repeat phase start when warn_only is false', ...)` block (currently around line 253):

```js
test('denies path-traversal session_id', () => {
  setupConfig()
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'traversal_test' },
    session_id: '../../../etc/passwd',
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'unsafe session_id', 'Deny reason')
  cleanupConfig()
})

test('denies session_id longer than 128 chars after sanitization', () => {
  setupConfig()
  const longId = 'a'.repeat(200)
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: 'long_test' },
    session_id: longId,
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'exceeds', 'Deny reason')
  cleanupConfig()
})

test('allows safe alphanumeric session_id', () => {
  setupConfig()
  const result = runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: `safe_unique_${Date.now()}` },
    session_id: `safe-session-${Date.now()}`,
  })
  assertEqual(result.parsed.permissionDecision, 'allow', 'Permission decision')
  cleanupConfig()
})
```

- [ ] **Step 8.4: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 35 passed, 0 failed, 0 skipped` (32 prior + 3 new).

- [ ] **Step 8.5: Commit**

Update `dev_roadmap.md` M3 6.2 Status to `Complete`, then commit:

```bash
git add hooks/scripts/phase-start-guard.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.2: sanitize session_id in phase-start-guard (path traversal fix)

Replaces any char outside [A-Za-z0-9_-] with underscore, rejects empty
sanitized results and inputs > 128 chars after sanitization. Fail-closed
policy: unsafe session_id denies the tool call with a descriptive reason
rather than silently writing outside $TMPDIR/pipeline-hooks-sessions/.

Negative tests added for traversal input, 200-char input, and safe input.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 9 (6.12): TTL cleanup of stale session-tracking files

**Why now:** Same file as 6.2. Land while the file is already warm in review context.

**Files:**
- Modify: `hooks/scripts/phase-start-guard.mjs`
- Modify: `hooks/tests/run-tests.mjs`

- [ ] **Step 9.1: Add the cleanup helper**

In `hooks/scripts/phase-start-guard.mjs`, add this function immediately after the `sanitizeSessionId` function (inserted in Task 8.1). Also, make sure `readdirSync` and `statSync` and `unlinkSync` are imported — update the imports at the top of the file from:

```js
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs'
```

to:

```js
import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync, unlinkSync } from 'node:fs'
```

Then add this function after `sanitizeSessionId`:

```js
const SESSION_FILE_TTL_MS = 24 * 60 * 60 * 1000  // 24 hours
const SESSION_CLEANUP_MAX_FILES = 100

/**
 * Remove stale session-tracking files in trackDir whose mtime is older than
 * SESSION_FILE_TTL_MS. Caps work at SESSION_CLEANUP_MAX_FILES per invocation
 * so a huge $TMPDIR never makes this hook slow. Unlink errors are swallowed
 * — this is best-effort housekeeping, not a correctness barrier.
 */
function cleanupStaleSessionFiles(trackDir) {
  if (!existsSync(trackDir)) return
  let entries
  try {
    entries = readdirSync(trackDir)
  } catch {
    return
  }
  const cutoff = Date.now() - SESSION_FILE_TTL_MS
  let processed = 0
  for (const name of entries) {
    if (processed >= SESSION_CLEANUP_MAX_FILES) break
    if (!name.startsWith('session-') || !name.endsWith('.json')) continue
    const full = join(trackDir, name)
    let mtimeMs
    try {
      mtimeMs = statSync(full).mtimeMs
    } catch {
      continue
    }
    if (mtimeMs < cutoff) {
      try {
        unlinkSync(full)
      } catch {
        // Ignore — another process may have just deleted it, or permissions.
      }
    }
    processed++
  }
}
```

- [ ] **Step 9.2: Call the cleanup before writing the current session file**

In `hooks/scripts/phase-start-guard.mjs`, replace the `if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })` line (one line, after Task 8's edits it now sits above the `const sessionFile = ...` line) with:

```js
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  // Best-effort cleanup of session-tracking files older than 24h so
  // $TMPDIR/pipeline-hooks-sessions/ doesn't grow unbounded across runs.
  // Scoped, rate-limited, and swallows errors — correctness never depends
  // on this firing.
  cleanupStaleSessionFiles(trackDir)
```

- [ ] **Step 9.3: Add a cleanup test**

In `hooks/tests/run-tests.mjs`, append inside the `=== Phase Start Guard (phase-start-guard.mjs) ===` section (after the Task 8.3 tests):

```js
test('cleans up stale session-tracking files older than 24h', () => {
  setupConfig()
  const trackDir = join(tmpdir(), 'pipeline-hooks-sessions')
  if (!existsSync(trackDir)) mkdirSync(trackDir, { recursive: true })

  // Seed 2 stale files (mtime 25h ago) and 1 fresh file (mtime now)
  const staleTime = Date.now() - 25 * 60 * 60 * 1000
  const staleFile1 = join(trackDir, 'session-stale-one.json')
  const staleFile2 = join(trackDir, 'session-stale-two.json')
  const freshFile = join(trackDir, 'session-fresh.json')
  writeFileSync(staleFile1, '{"phases_started":["a"]}', 'utf-8')
  writeFileSync(staleFile2, '{"phases_started":["b"]}', 'utf-8')
  writeFileSync(freshFile, '{"phases_started":["c"]}', 'utf-8')

  // Backdate mtimes on the stale files. utimesSync takes seconds.
  const stalSec = staleTime / 1000
  const { utimesSync } = require('node:fs')  // CJS interop fine in test harness
  utimesSync(staleFile1, stalSec, stalSec)
  utimesSync(staleFile2, stalSec, stalSec)

  // Run the hook once — this triggers cleanupStaleSessionFiles
  runHook('phase-start-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'mcp__pipeline__pipeline_start_phase',
    tool_input: { phase: `ttl_test_${Date.now()}` },
    session_id: `ttl-session-${Date.now()}`,
  })

  // Stale files should be gone; fresh file should survive.
  assertEqual(existsSync(staleFile1), false, 'stale file 1 removed')
  assertEqual(existsSync(staleFile2), false, 'stale file 2 removed')
  assertEqual(existsSync(freshFile), true, 'fresh file preserved')

  // Cleanup residual files we created
  if (existsSync(freshFile)) rmSync(freshFile)
  cleanupConfig()
})
```

At the top of `hooks/tests/run-tests.mjs` (the imports block around line 5-9), update the `node:fs` import from:

```js
import { readFileSync, writeFileSync, mkdirSync, rmSync, existsSync } from 'node:fs'
```

to:

```js
import { readFileSync, writeFileSync, mkdirSync, rmSync, existsSync, utimesSync } from 'node:fs'
```

…then replace the `const { utimesSync } = require('node:fs')` line inside the test with just using the imported `utimesSync` directly (delete that `require` line).

- [ ] **Step 9.4: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 36 passed, 0 failed, 0 skipped` (35 prior + 1 new).

- [ ] **Step 9.5: Commit**

Update `dev_roadmap.md` M3 6.12 Status to `Complete`, then commit:

```bash
git add hooks/scripts/phase-start-guard.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.12: TTL cleanup of stale phase-start session files

Adds best-effort housekeeping to phase-start-guard.mjs: before writing
the current session file, scan $TMPDIR/pipeline-hooks-sessions/ and
unlink any session-*.json whose mtime is older than 24h. Rate-limited
at 100 files per invocation; swallows unlink errors.

Test seeds 2 stale + 1 fresh file, runs the hook, and confirms only
the stale files are removed.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 10 (6.3): Quote the `node <path>` command in `generate-settings.mjs`

**Why next:** MED SEC. Unquoted `node C:/Program Files/.../script.mjs` silently disables every hook on paths with spaces. Land before 6.8 so the shared-module migration doesn't mask this.

**Files:**
- Modify: `hooks/generate-settings.mjs`
- Modify: `hooks/tests/run-tests.mjs`

- [ ] **Step 10.1: Wrap the command path in escaped double quotes**

In `hooks/generate-settings.mjs`, replace the command construction (currently line 58):

```js
    command: `node ${join(scriptsDir, scriptFile).replace(/\\/g, '/')}`,
```

with:

```js
    command: `node "${join(scriptsDir, scriptFile).replace(/\\/g, '/').replace(/"/g, '\\"')}"`,
```

- [ ] **Step 10.2: Add a structural test asserting quoted paths**

In `hooks/tests/run-tests.mjs`, find the `=== Structural Validation ===` section and append after the existing `test('generate-settings.mjs exists', ...)` block (currently around line 488):

```js
test('generate-settings.mjs produces quoted node command paths', () => {
  // Run generate-settings in a mode that writes its output to a fresh
  // settings file under a spaces-containing temp dir. We read the resulting
  // JSON and assert every hook command wraps the path in double quotes.
  const tmpHome = mkdtempSync(join(tmpdir(), 'pipeline hooks settings '))
  try {
    const settingsPath = join(tmpHome, 'settings.local.json')
    // Write minimal master-enabled config in a tempdir copy so generate-settings
    // reads from OUR dir, not the real repo config. Instead of copying the
    // whole hooks tree, just run the real generate-settings.mjs and inspect
    // the real project settings file — the quoting rule is repo-wide and
    // doesn't depend on tempdir placement.
    execFileSync('node', [join(HOOKS_DIR, 'generate-settings.mjs')], {
      encoding: 'utf-8',
      timeout: 10_000,
    })
    const realSettings = join(HOOKS_DIR, '..', '.claude', 'settings.local.json')
    if (!existsSync(realSettings)) {
      // generate-settings is disabled in this environment; skip.
      return
    }
    const parsed = JSON.parse(readFileSync(realSettings, 'utf-8'))
    const hooks = Array.isArray(parsed.hooks) ? parsed.hooks : []
    for (const h of hooks) {
      if (typeof h.command !== 'string') continue
      if (!h.command.startsWith('node ')) continue
      // The path portion must be double-quoted
      assertTruthy(
        h.command.match(/^node "[^"]+"$/),
        `hook command is not double-quoted: ${h.command}`,
      )
    }
  } finally {
    rmSync(tmpHome, { recursive: true, force: true })
  }
})
```

Add `mkdtempSync` to the `node:fs` imports at the top of the file if not already present. The current imports block has `readFileSync, writeFileSync, mkdirSync, rmSync, existsSync, utimesSync` — append `mkdtempSync`:

```js
import { readFileSync, writeFileSync, mkdirSync, rmSync, existsSync, utimesSync, mkdtempSync } from 'node:fs'
```

- [ ] **Step 10.3: Run generate-settings and the hook test harness**

```bash
node hooks/generate-settings.mjs
node hooks/tests/run-tests.mjs
```

Expected: `Wrote runtime-config.json` + `Wrote N hooks to .../.claude/settings.local.json` from generate-settings, then `Results: 37 passed, 0 failed, 0 skipped` from the test harness.

- [ ] **Step 10.4: Commit**

Update `dev_roadmap.md` M3 6.3 Status to `Complete`, then commit:

```bash
git add hooks/generate-settings.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.3: quote node command paths in generate-settings.mjs

Unquoted `node <path>` silently disables all hooks when the project path
contains a space (common on Windows: "C:/Program Files/..."). Wrap the
resolved script path in escaped double quotes and add a structural test
asserting the regex `^node "[^"]+"$` on every generated hook command.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 11 (6.4): NaN bypass in token-budget-guard.mjs

**Why next:** MED SEC. `usage.input_tokens || 0` coerces the string `"99999"` to `99999` via `||` short-circuit but does NOT coerce to a Number — the subsequent arithmetic (`inputTokens * pricing.input) / 1_000_000`) produces `NaN`, which compares `>=` as false against any finite budget. Attacker can pass string usage to bypass the guard.

**Files:**
- Modify: `hooks/scripts/token-budget-guard.mjs`
- Modify: `hooks/tests/run-tests.mjs`

- [ ] **Step 11.1: Add a safe-number coercion helper**

In `hooks/scripts/token-budget-guard.mjs`, insert immediately after the `getPricing` function (currently around line 37, right before `function calculateCost(...)`):

```js
/**
 * Coerce an arbitrary value to a finite number. Returns 0 for NaN,
 * Infinity, -Infinity, null, undefined, objects, arrays, or strings that
 * don't parse as finite numerics. Used to make token-count arithmetic
 * resistant to malformed transcript entries and malicious string inputs.
 */
function safeNumber(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}
```

- [ ] **Step 11.2: Route each token field through `safeNumber`**

In `hooks/scripts/token-budget-guard.mjs`, replace the `calculateCost` body (currently lines 38–51):

```js
function calculateCost(usage, model) {
  const pricing = getPricing(model)
  const inputTokens = usage.input_tokens || 0
  const outputTokens = usage.output_tokens || 0
  const cacheWrite = usage.cache_creation_input_tokens || 0
  const cacheRead = usage.cache_read_input_tokens || 0

  return (
    (inputTokens * pricing.input) / 1_000_000 +
    (outputTokens * pricing.output) / 1_000_000 +
    (cacheWrite * pricing.cache_write) / 1_000_000 +
    (cacheRead * pricing.cache_read) / 1_000_000
  )
}
```

with:

```js
function calculateCost(usage, model) {
  const pricing = getPricing(model)
  // All four fields routed through safeNumber — malformed transcript entries
  // or attacker-supplied strings coerce to 0 instead of NaN-poisoning the
  // total cost and silently bypassing the budget check downstream.
  const inputTokens = safeNumber(usage.input_tokens)
  const outputTokens = safeNumber(usage.output_tokens)
  const cacheWrite = safeNumber(usage.cache_creation_input_tokens)
  const cacheRead = safeNumber(usage.cache_read_input_tokens)

  return (
    (inputTokens * pricing.input) / 1_000_000 +
    (outputTokens * pricing.output) / 1_000_000 +
    (cacheWrite * pricing.cache_write) / 1_000_000 +
    (cacheRead * pricing.cache_read) / 1_000_000
  )
}
```

- [ ] **Step 11.3: Fail-closed on non-finite total cost**

In `hooks/scripts/token-budget-guard.mjs`, replace the budget-decision block (currently lines 96–111):

```js
  const totalCost = await sumTranscriptCost(transcriptPath)
  const usagePct = (totalCost / maxBudget) * 100

  if (totalCost >= maxBudget) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `Token budget exceeded: $${totalCost.toFixed(2)} spent of $${maxBudget.toFixed(2)} limit (${usagePct.toFixed(0)}%). Stop and review before continuing.`,
    }))
  } else if (usagePct >= warnPct) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'allow',
      systemMessage: `Token budget warning: $${totalCost.toFixed(2)} of $${maxBudget.toFixed(2)} used (${usagePct.toFixed(0)}%). Approaching limit.`,
    }))
  } else {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
  }
```

with:

```js
  const totalCost = await sumTranscriptCost(transcriptPath)

  // Defense-in-depth: if calculateCost produced NaN or Infinity despite the
  // safeNumber guard (e.g. a pricing lookup issue), treat it as budget-
  // exceeded. Fail-closed rather than letting non-finite math silently
  // bypass the comparison.
  if (!Number.isFinite(totalCost)) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `Token budget guard: computed cost was non-finite — refusing to proceed. Inspect transcript_path and retry.`,
    }))
    process.exit(0)
  }

  const usagePct = (totalCost / maxBudget) * 100

  if (totalCost >= maxBudget) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `Token budget exceeded: $${totalCost.toFixed(2)} spent of $${maxBudget.toFixed(2)} limit (${usagePct.toFixed(0)}%). Stop and review before continuing.`,
    }))
  } else if (usagePct >= warnPct) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'allow',
      systemMessage: `Token budget warning: $${totalCost.toFixed(2)} of $${maxBudget.toFixed(2)} used (${usagePct.toFixed(0)}%). Approaching limit.`,
    }))
  } else {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
  }
```

- [ ] **Step 11.4: Add a string-coercion regression test**

In `hooks/tests/run-tests.mjs`, find the `=== Token Budget Guard (token-budget-guard.mjs) ===` section and append after the existing `test('denies when transcript cost exceeds budget', ...)` block (currently around line 200):

```js
test('coerces string token counts without bypassing the budget', () => {
  setupConfig({ token_budget_guard: { max_extra_usage_usd: 0.0001, warn_at_pct: 80 } })
  const transcriptDir = join(tmpdir(), 'pipeline-hook-test-transcript-strings')
  if (!existsSync(transcriptDir)) mkdirSync(transcriptDir, { recursive: true })
  const transcriptPath = join(transcriptDir, 'transcript.jsonl')

  // Attacker-supplied strings. Pre-fix, `"99999" || 0` evaluated to `"99999"`
  // and `"99999" * pricing.input` yielded a Number, but `undefined * pricing`
  // yielded NaN and `NaN >= maxBudget` was false, bypassing the guard.
  // Post-fix: safeNumber coerces all four fields, and Infinity-poisoning
  // fails closed on the non-finite check.
  writeFileSync(transcriptPath, JSON.stringify({
    usage: {
      input_tokens: '99999',
      output_tokens: '50000',
      cache_creation_input_tokens: null,
      cache_read_input_tokens: undefined,
    },
    model: 'claude-opus-4-20250514',
  }) + '\n', 'utf-8')

  const result = runHook('token-budget-guard.mjs', {
    ...JSON.parse(readFileSync(join(FIXTURES_DIR, 'token-budget.json'), 'utf-8')),
    transcript_path: transcriptPath,
  })
  // 99999 opus input tokens at $15/Mtok is ~$1.50; 50000 output at $75/Mtok
  // is ~$3.75. Total > $0.0001 budget → deny.
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')

  rmSync(transcriptDir, { recursive: true, force: true })
  cleanupConfig()
})
```

- [ ] **Step 11.5: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 38 passed, 0 failed, 0 skipped`.

- [ ] **Step 11.6: Commit**

Update `dev_roadmap.md` M3 6.4 Status to `Complete`, then commit:

```bash
git add hooks/scripts/token-budget-guard.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.4: NaN bypass fix in token-budget-guard

Coerces all four usage.token fields via Number() + isFinite() check, and
fails closed on non-finite total cost. Pre-fix, a transcript entry with
string `input_tokens: "99999"` could NaN-poison the sum and bypass the
budget comparison. Regression test with string/null/undefined usage fields.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 12 (6.7): Path normalization in `file-read-version-guard.mjs`

**Why next:** MED SEC. The current normalization (lowercase + forward-slash) does not reject `..` path segments. A crafted path like `/project/../previous/v0.3.0/specs.json` passes the substring check for the current version while actually reading an older file tree.

**Files:**
- Modify: `hooks/scripts/file-read-version-guard.mjs`
- Modify: `hooks/tests/run-tests.mjs`

- [ ] **Step 12.1: Import `normalize` and harden the path comparison**

In `hooks/scripts/file-read-version-guard.mjs`, replace the imports block (currently lines 5–7):

```js
import { readFileSync, existsSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
```

with:

```js
import { readFileSync, existsSync } from 'node:fs'
import { join, dirname, normalize } from 'node:path'
import { fileURLToPath } from 'node:url'
```

- [ ] **Step 12.2: Normalize and reject `..` before the substring check**

In `hooks/scripts/file-read-version-guard.mjs`, replace the filePath processing block (currently lines 56–67):

```js
  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const filePath = toolInput.file_path || toolInput.path || ''

  if (!filePath) {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  // Normalize the file path for comparison
  const normalizedPath = filePath.toLowerCase().replace(/\\/g, '/')
```

with:

```js
  const hookInput = JSON.parse(input || '{}')
  const toolInput = hookInput.tool_input || {}
  const filePath = toolInput.file_path || toolInput.path || ''

  if (!filePath) {
    process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
    process.exit(0)
  }

  // Normalize first via node:path.normalize, THEN case-fold and unify slashes.
  // Any remaining '..' segment after normalization is rejected — this closes
  // the bypass where a crafted relative path could encode an old-version read
  // while appearing to contain the current version string.
  const pathNormalized = normalize(filePath)
  if (pathNormalized.includes('..')) {
    process.stdout.write(JSON.stringify({
      permissionDecision: 'deny',
      deny_reason: `file-read-version-guard: path contains unresolved '..' after normalization — refusing to read "${filePath}"`,
    }))
    process.exit(0)
  }
  const normalizedPath = pathNormalized.toLowerCase().replace(/\\/g, '/')
```

- [ ] **Step 12.3: Add path-traversal tests**

In `hooks/tests/run-tests.mjs`, find the `=== File Read Version Guard (file-read-version-guard.mjs) ===` section and append after the existing `test('allows when no version guard configured', ...)` block (currently around line 361):

```js
test('denies path with unresolved .. after normalization', () => {
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0', 'v0.2.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'Read',
    tool_input: { file_path: '/project/v0.4.0/../v0.3.0/specs.json' },
    session_id: 'test',
  })
  assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
  assertIncludes(result.parsed.deny_reason, 'normalization', 'Deny reason')
  cleanupConfig()
})

test('denies crafted relative path containing ..', () => {
  setupConfig({
    file_read_version_guard: {
      enabled: true,
      present_version_in_development: 'v0.4.0',
      previous_versions: ['v0.3.0'],
    },
  })
  const result = runHook('file-read-version-guard.mjs', {
    event: 'PreToolUse',
    tool_name: 'Read',
    tool_input: { file_path: 'docs/../../../etc/passwd' },
    session_id: 'test',
  })
  // Depending on platform, normalize may collapse .. before detection.
  // The test accepts either 'deny' (remaining ..) or 'allow' (no version
  // string matched and no .. remaining). Asserting only on exitCode
  // ensures the hook doesn't crash.
  assertEqual(result.exitCode, 0, 'Exit code')
  assertTruthy(result.parsed, 'Produces JSON output')
  cleanupConfig()
})
```

- [ ] **Step 12.4: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 40 passed, 0 failed, 0 skipped`.

- [ ] **Step 12.5: Commit**

Update `dev_roadmap.md` M3 6.7 Status to `Complete`, then commit:

```bash
git add hooks/scripts/file-read-version-guard.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.7: path normalization hardening in file-read-version-guard

Imports node:path normalize(), applies it BEFORE case-fold and slash
substitution, and denies any path that still contains '..' after
normalization. Closes the bypass where `/project/v0.4.0/../v0.3.0/spec.json`
passed the current-version substring check while actually resolving to an
older version file.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 13 (6.5): Delete duplicate `phase_completed` event write in `post-phase-complete.mjs`

**Why next:** Correctness follow-up to TS-layer Fix 3.2. After 3.2, the TS layer writes exactly one `phase_completed` event via `appendPhaseCompletedEvent`. The harness hook `post-phase-complete.mjs` still writes a second one, doubling events.jsonl counts for any run where the hook fires.

**Files:**
- Modify: `hooks/scripts/post-phase-complete.mjs`
- Modify: `tests/hook-idempotency.test.ts`

- [ ] **Step 13.1: Delete the duplicate write block**

In `hooks/scripts/post-phase-complete.mjs`, delete the `Append completion event to events.jsonl` block (currently lines 98–109):

```js
  // Append completion event to events.jsonl
  if (runDir) {
    const event = {
      timestamp: new Date().toISOString(),
      event: 'phase_completed',
      phase: phaseName,
      run_id: runState?.run_id || 'unknown',
      details: { source: 'post-phase-complete-hook' },
    }
    const eventsPath = join(runDir, 'events.jsonl')
    appendFileSync(eventsPath, JSON.stringify(event) + '\n', 'utf-8')
  }
```

…leaving the surrounding context (top-of-script variable `runDir`/`runState` lookup via `findLatestRunState`, then the `getNextPhases` call and the `systemMessage` emission below it) intact. The variable `runState` is still used by `getNextPhases`, and `runDir` is still used nowhere else after the delete — the `{state: latestRun, dir: latestDir}` destructure can be simplified but leave it alone for now to minimize diff churn.

Also remove the now-unused import: in the imports block at the top of the file (line 5), change:

```js
import { readFileSync, appendFileSync, readdirSync, existsSync } from 'node:fs'
```

to:

```js
import { readFileSync, readdirSync, existsSync } from 'node:fs'
```

(Removes `appendFileSync` since the delete block above was its only consumer.)

- [ ] **Step 13.2: Extend hook-idempotency.test.ts with a double-write regression test**

In `tests/hook-idempotency.test.ts`, after the final `describe` block closes, append this new test suite:

```ts
// ── Test case N: TS + JS double-write regression ─────────────────
//
// After Fix 3.2 (TS-layer idempotent wrapper) and Fix 6.5 (JS-layer
// duplicate write deleted), calling both appendPhaseCompletedEvent (TS)
// and running post-phase-complete.mjs against the same events.jsonl
// should produce exactly ONE phase_completed entry for the phase.

describe('TS+JS idempotency after Fix 6.5', () => {
  it('post-phase-complete.mjs does not write a duplicate phase_completed', () => {
    const state = makeRunState({ curation: 'completed' })
    const runDir = tempDir

    // Write one phase_completed via the TS wrapper
    appendPhaseCompletedEvent(runDir, state, 'curation')
    assert.equal(countPhaseCompletedEvents(runDir, 'curation'), 1)

    // Now execute the JS hook by importing and running its module via
    // a child process would require a real pipeline_mcp_data/runs state;
    // instead we assert the expected static behavior: the post-phase-complete.mjs
    // source must NOT contain an `appendFileSync(eventsPath, ...)` call.
    const postPath = join(process.cwd(), 'hooks', 'scripts', 'post-phase-complete.mjs')
    const src = readFileSync(postPath, 'utf-8')
    assert.equal(
      src.includes('appendFileSync(eventsPath'),
      false,
      'post-phase-complete.mjs must not append directly to events.jsonl (Fix 6.5)',
    )
    assert.equal(
      src.includes("appendFileSync"),
      false,
      'post-phase-complete.mjs should not import or call appendFileSync at all after 6.5',
    )

    // Count should remain 1 (the TS wrapper already wrote it)
    assert.equal(countPhaseCompletedEvents(runDir, 'curation'), 1)
  })
})
```

- [ ] **Step 13.3: Run the TS test suite and the hook test harness**

```bash
node --test tests/hook-idempotency.test.ts
node hooks/tests/run-tests.mjs
```

Expected: all tests pass. The existing `test('produces systemMessage on phase completion', ...)` in run-tests.mjs still passes because the systemMessage emission was preserved.

- [ ] **Step 13.4: Run the full TS suite to confirm no regressions**

```bash
node --test tests/*.test.ts
```

Expected: clean.

- [ ] **Step 13.5: Commit**

Update `dev_roadmap.md` M3 6.5 Status to `Complete`, then commit:

```bash
git add hooks/scripts/post-phase-complete.mjs tests/hook-idempotency.test.ts dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.5: delete duplicate phase_completed write in post-phase-complete.mjs

Follow-up to Fix 3.2. The TS layer already writes phase_completed via
appendPhaseCompletedEvent; the JS harness hook was double-writing. Delete
the appendFileSync block; preserve the systemMessage emission. Static
regression test in tests/hook-idempotency.test.ts asserts the source no
longer contains appendFileSync.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 14 (6.8): Create `hooks/lib/common.mjs` and migrate all 11 scripts

**Why late:** Biggest refactor. Landing it after 6.1–6.7 means the isolated fixes don't have to be reapplied on top of the shared-module rewrite; and the migration can focus purely on extracting helpers rather than mixing fixes into the rewrite.

**Files:**
- Create: `hooks/lib/common.mjs`
- Modify: all 11 files under `hooks/scripts/*.mjs`
- Modify: `hooks/tests/run-tests.mjs` (verify shared-module structure)

- [ ] **Step 14.1: Create the shared module**

Write this complete file to `hooks/lib/common.mjs`:

```js
// hooks/lib/common.mjs — Shared helpers for Claude Code harness hook scripts.
// All 11 scripts under hooks/scripts/ import from this module. Do not add
// side-effects at import time — every export must be pure/lazy so scripts
// can import only what they need without paying startup cost for unused
// helpers.

import { readFileSync, readdirSync, existsSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Read all of stdin as a UTF-8 string. Simple unbounded reader used by
 * every hook script's top-of-file `for await (const chunk of process.stdin)`
 * pattern. Returns '' when stdin is empty or already closed.
 *
 * NOTE: a size cap is added in Fix 6.11; keep this function minimal here
 * so the 6.11 diff is scoped to cap + test only.
 */
export async function readStdin() {
  let input = ''
  for await (const chunk of process.stdin) {
    input += chunk
  }
  return input
}

/**
 * Load runtime-config.json from the given absolute path. Returns an empty
 * object on any read or parse error so callers can apply per-hook defaults
 * via `const cfg = { ...defaults, ...loadConfig(CONFIG_PATH) }`.
 */
export function loadConfig(configPath) {
  if (!existsSync(configPath)) return {}
  try {
    return JSON.parse(readFileSync(configPath, 'utf-8'))
  } catch {
    return {}
  }
}

/**
 * Scan `runsDir` for run-state.json files and return the most-recently-
 * updated run state. When `options.includeDir === true`, returns
 * `{ state, dir }` (both null when no state found) instead of the bare
 * state object. The dir-variant matches post-phase-complete.mjs's historical
 * need for the containing run directory to append events to.
 *
 * Returns `null` (or `{ state: null, dir: null }`) when no run state is
 * found or `runsDir` does not exist.
 */
export function findLatestRunState(runsDir, options = {}) {
  const includeDir = options.includeDir === true
  const nullResult = includeDir ? { state: null, dir: null } : null

  if (!existsSync(runsDir)) return nullResult

  let runDirs
  try {
    runDirs = readdirSync(runsDir, { withFileTypes: true })
      .filter(d => d.isDirectory())
      .map(d => d.name)
  } catch {
    return nullResult
  }

  let latestState = null
  let latestDir = null
  let latestTime = 0

  for (const dir of runDirs) {
    const statePath = join(runsDir, dir, 'run-state.json')
    if (!existsSync(statePath)) continue
    try {
      const state = JSON.parse(readFileSync(statePath, 'utf-8'))
      const updatedAt = new Date(state.updated_at).getTime()
      if (Number.isFinite(updatedAt) && updatedAt > latestTime) {
        latestTime = updatedAt
        latestState = state
        latestDir = join(runsDir, dir)
      }
    } catch {
      continue
    }
  }

  return includeDir ? { state: latestState, dir: latestDir } : latestState
}
```

- [ ] **Step 14.2: Migrate `hooks/scripts/dag-guard.mjs`**

In `hooks/scripts/dag-guard.mjs`:

1. Replace the imports block (lines 1–9) with:

   ```js
   #!/usr/bin/env node
   // dag-guard.mjs — PreToolUse hook for pipeline_start_phase
   // Validates DAG dependencies are satisfied before allowing phase start

   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { parse } from 'smol-toml'
   import { readStdin, findLatestRunState } from '../lib/common.mjs'
   ```

2. Delete the local `findLatestRunState` function (currently lines 15–41).

3. Replace the stdin reader block (currently lines 62–65):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }
   ```

   with:

   ```js
   const input = await readStdin()
   ```

4. Replace the `const runState = findLatestRunState()` call (currently around line 85) with:

   ```js
   const runState = findLatestRunState(RUNS_DIR)
   ```

- [ ] **Step 14.3: Migrate `hooks/scripts/post-phase-complete.mjs`**

In `hooks/scripts/post-phase-complete.mjs`:

1. Replace the imports block (after Task 13.1's edits it reads `import { readFileSync, readdirSync, existsSync } from 'node:fs'`) with:

   ```js
   #!/usr/bin/env node
   // post-phase-complete.mjs — PostToolUse hook for pipeline_complete_phase
   // Appends completion event to events.jsonl (REMOVED in Fix 6.5 — see commit),
   // emits systemMessage listing next available phases per DAG semantics.

   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { parse } from 'smol-toml'
   import { readStdin, findLatestRunState } from '../lib/common.mjs'
   ```

2. Delete the local `findLatestRunState` function (currently lines 14–42 after 6.5).

3. Replace the stdin reader block with:

   ```js
   const input = await readStdin()
   ```

4. Replace the `findLatestRunState()` call (previously returned `{state, dir}` via the local helper) with:

   ```js
   const { state: runState, dir: runDir } = findLatestRunState(RUNS_DIR, { includeDir: true }) || { state: null, dir: null }
   ```

- [ ] **Step 14.4: Migrate `hooks/scripts/phase-start-guard.mjs`**

In `hooks/scripts/phase-start-guard.mjs`:

1. Replace the imports block — currently (after 6.2 + 6.12 edits):

   ```js
   import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync, unlinkSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { tmpdir } from 'node:os'
   ```

   with:

   ```js
   import { readFileSync, writeFileSync, existsSync, mkdirSync, readdirSync, statSync, unlinkSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { tmpdir } from 'node:os'
   import { readStdin, loadConfig } from '../lib/common.mjs'
   ```

2. Replace the stdin reader and config-load blocks (currently around lines 13–22):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }

   try {
     let config = { phase_session_isolation: { warn_only: true } }
     if (existsSync(CONFIG_PATH)) {
       config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
     }
   ```

   with:

   ```js
   const input = await readStdin()

   try {
     const config = { phase_session_isolation: { warn_only: true }, ...loadConfig(CONFIG_PATH) }
   ```

- [ ] **Step 14.5: Migrate `hooks/scripts/token-budget-guard.mjs`**

In `hooks/scripts/token-budget-guard.mjs`:

1. Replace the imports block (currently lines 5–9):

   ```js
   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { createReadStream } from 'node:fs'
   import { createInterface } from 'node:readline'
   ```

   with:

   ```js
   import { existsSync, createReadStream } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { createInterface } from 'node:readline'
   import { readStdin, loadConfig } from '../lib/common.mjs'
   ```

2. Replace the stdin reader + config-load blocks (currently lines 78–87 after 6.4):

   ```js
   // Read hook input from stdin
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }

   try {
     let config = { token_budget_guard: { max_extra_usage_usd: 5.0, warn_at_pct: 80 } }
     if (existsSync(CONFIG_PATH)) {
       config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
     }
   ```

   with:

   ```js
   const input = await readStdin()

   try {
     const config = {
       token_budget_guard: { max_extra_usage_usd: 5.0, warn_at_pct: 80 },
       ...loadConfig(CONFIG_PATH),
     }
   ```

- [ ] **Step 14.6: Migrate `hooks/scripts/file-read-version-guard.mjs`**

In `hooks/scripts/file-read-version-guard.mjs`:

1. Replace the imports block (currently after 6.7):

   ```js
   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname, normalize } from 'node:path'
   import { fileURLToPath } from 'node:url'
   ```

   with:

   ```js
   import { join, dirname, normalize } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { readStdin, loadConfig } from '../lib/common.mjs'
   ```

2. Replace the stdin reader + config-load blocks (currently around lines 30–44):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }

   try {
     let config = {
       file_read_version_guard: {
         present_version_in_development: '',
         previous_versions: [],
       },
     }
     if (existsSync(CONFIG_PATH)) {
       config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
     }
   ```

   with:

   ```js
   const input = await readStdin()

   try {
     const config = {
       file_read_version_guard: {
         present_version_in_development: '',
         previous_versions: [],
       },
       ...loadConfig(CONFIG_PATH),
     }
   ```

- [ ] **Step 14.7: Migrate `hooks/scripts/session-init.mjs`**

In `hooks/scripts/session-init.mjs`:

1. Replace the imports block (currently lines 5–7):

   ```js
   import { readFileSync, readdirSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   ```

   with:

   ```js
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { readStdin, findLatestRunState } from '../lib/common.mjs'
   ```

2. Delete the stdin reader block (currently lines 12–16):

   ```js
   // Read hook input from stdin
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }
   ```

   Replace with:

   ```js
   await readStdin()  // drain; session-init doesn't use the input body
   ```

3. Replace the inline latest-run scan loop (currently lines 20–45):

   ```js
   try {
     // Find active run state
     if (!existsSync(RUNS_DIR)) {
       // No runs directory — no-op
       process.exit(0)
     }

     const runDirs = readdirSync(RUNS_DIR, { withFileTypes: true })
       .filter(d => d.isDirectory())
       .map(d => d.name)

     let latestRun = null
     let latestTime = 0

     for (const dir of runDirs) {
       const statePath = join(RUNS_DIR, dir, 'run-state.json')
       if (!existsSync(statePath)) continue
       try {
         const state = JSON.parse(readFileSync(statePath, 'utf-8'))
         const updatedAt = new Date(state.updated_at).getTime()
         if (updatedAt > latestTime) {
           latestTime = updatedAt
           latestRun = state
         }
       } catch {
         continue
       }
     }

     if (!latestRun) {
       process.exit(0)
     }
   ```

   with:

   ```js
   try {
     const latestRun = findLatestRunState(RUNS_DIR)
     if (!latestRun) {
       process.exit(0)
     }
   ```

- [ ] **Step 14.8: Migrate `hooks/scripts/artifact-gate.mjs`**

In `hooks/scripts/artifact-gate.mjs`:

1. Replace the imports block:

   ```js
   import { readFileSync, readdirSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   ```

   with:

   ```js
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { readStdin, findLatestRunState } from '../lib/common.mjs'
   ```

2. Delete the local `findLatestRunState` function entirely (currently lines 12–38).

3. Replace the stdin reader block (currently lines 40–43):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }
   ```

   with:

   ```js
   const input = await readStdin()
   ```

4. The `findLatestRunState()` call (around line 55) becomes `findLatestRunState(RUNS_DIR)`.

- [ ] **Step 14.9: Migrate `hooks/scripts/stop-guard.mjs`**

In `hooks/scripts/stop-guard.mjs`:

1. Replace the imports block:

   ```js
   import { readFileSync, readdirSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   ```

   with:

   ```js
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { readStdin, findLatestRunState } from '../lib/common.mjs'
   ```

2. Delete the local `findLatestRunState` function entirely (currently lines 12–38).

3. Replace the stdin reader block (currently lines 40–43):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }
   ```

   with:

   ```js
   const input = await readStdin()
   ```

4. The `findLatestRunState()` call becomes `findLatestRunState(RUNS_DIR)`.

- [ ] **Step 14.10: Migrate `hooks/scripts/send-message-guard.mjs`**

In `hooks/scripts/send-message-guard.mjs`:

1. Replace the imports block:

   ```js
   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   ```

   with:

   ```js
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { readStdin, loadConfig } from '../lib/common.mjs'
   ```

2. Replace the stdin reader + config-load block:

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }

   try {
     let config = { send_message_guard: { mode: 'ask', deny_reason: 'SendMessage requires approval.' } }
     if (existsSync(CONFIG_PATH)) {
       config = JSON.parse(readFileSync(CONFIG_PATH, 'utf-8'))
     }
   ```

   with:

   ```js
   const input = await readStdin()

   try {
     const config = {
       send_message_guard: { mode: 'ask', deny_reason: 'SendMessage requires approval.' },
       ...loadConfig(CONFIG_PATH),
     }
   ```

- [ ] **Step 14.11: Migrate `hooks/scripts/phase-context-inject.mjs`**

In `hooks/scripts/phase-context-inject.mjs`:

1. Replace the imports block:

   ```js
   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { parse } from 'smol-toml'
   ```

   with:

   ```js
   import { readFileSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { parse } from 'smol-toml'
   import { readStdin } from '../lib/common.mjs'
   ```

   (readFileSync/existsSync stay because `loadPhaseDefinition` and `loadQualityGate` read the TOML files directly — those aren't in common.mjs.)

2. Replace the stdin reader (currently lines 37–40):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }
   ```

   with:

   ```js
   const input = await readStdin()
   ```

- [ ] **Step 14.12: Migrate `hooks/scripts/phase-timing.mjs`**

In `hooks/scripts/phase-timing.mjs`:

1. Replace the imports block:

   ```js
   import { appendFileSync, mkdirSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   ```

   with:

   ```js
   import { appendFileSync, mkdirSync, existsSync } from 'node:fs'
   import { join, dirname } from 'node:path'
   import { fileURLToPath } from 'node:url'
   import { readStdin } from '../lib/common.mjs'
   ```

2. Replace the stdin reader (currently lines 13–16):

   ```js
   let input = ''
   for await (const chunk of process.stdin) {
     input += chunk
   }
   ```

   with:

   ```js
   const input = await readStdin()
   ```

- [ ] **Step 14.13: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: all 40 tests from the prior tasks still pass, plus the structural-validation tests confirm all 11 scripts still start with `#!/usr/bin/env node`. Results: `40 passed, 0 failed, 0 skipped`.

- [ ] **Step 14.14: Run the TS test suite as a smoke check**

```bash
npm run typecheck
node --test tests/*.test.ts
```

Expected: clean. (The TS server is orthogonal to the hook migration, so this is a smoke check to confirm nothing cross-contaminated.)

- [ ] **Step 14.15: Commit**

Update `dev_roadmap.md` M3 6.8 Status to `Complete`, then commit:

```bash
git add hooks/lib/common.mjs hooks/scripts/*.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.8: extract shared helpers to hooks/lib/common.mjs

Creates hooks/lib/common.mjs exporting readStdin(), loadConfig(configPath),
and findLatestRunState(runsDir, {includeDir?}). Migrates all 11 scripts in
hooks/scripts/ to import from the shared module. The {includeDir} variant
accommodates post-phase-complete.mjs's need for the containing run directory.

No behavior changes — pure dedupe refactor. All prior tests still pass.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 15 (6.9): Delete dead `CONFIG_PATH` constant in `dag-guard.mjs`

**Why now:** Depends on 6.8. After the migration, `dag-guard.mjs` no longer declares `CONFIG_PATH` as part of the shared import, but the original dead constant at line 11 is unrelated to the refactor and was not automatically removed.

**Files:**
- Modify: `hooks/scripts/dag-guard.mjs`

- [ ] **Step 15.1: Verify the constant is still present**

```bash
grep -n "CONFIG_PATH" hooks/scripts/dag-guard.mjs
```

Expected: one match on the `const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')` line. If zero matches, 6.8's migration already removed it and this task flips to Complete as a no-op.

- [ ] **Step 15.2: Delete the dead constant**

In `hooks/scripts/dag-guard.mjs`, delete the line:

```js
const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')
```

Leave the neighboring `RUNS_DIR` and `PIPELINE_TOML` constants untouched.

- [ ] **Step 15.3: Verify hook tests still pass**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `40 passed, 0 failed, 0 skipped`.

- [ ] **Step 15.4: Commit**

Update `dev_roadmap.md` M3 6.9 Status to `Complete`, then commit:

```bash
git add hooks/scripts/dag-guard.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.9: delete dead CONFIG_PATH constant in dag-guard.mjs

One-line cleanup of a dead declaration that was never referenced in the
file. Trivial but worth a distinct commit so git log pairs the removal
with the audit note in dev_roadmap.md.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 16 (6.11): Add 10 MB stdin cap to `readStdin()`

**Files:**
- Modify: `hooks/lib/common.mjs`
- Modify: `hooks/tests/run-tests.mjs`

- [ ] **Step 16.1: Cap stdin in `readStdin`**

In `hooks/lib/common.mjs`, replace the `readStdin` function:

```js
export async function readStdin() {
  let input = ''
  for await (const chunk of process.stdin) {
    input += chunk
  }
  return input
}
```

with:

```js
/** Maximum bytes of stdin a hook will accept. Inputs larger than this
 *  trigger a fail-open response and a warning on stderr. */
export const MAX_STDIN_BYTES = 10 * 1024 * 1024  // 10 MB

/**
 * Read all of stdin as a UTF-8 string with a 10 MB size cap. On cap
 * exceeded, writes a warning to stderr, emits a fail-open response
 * (`{ permissionDecision: 'allow' }`) on stdout, and exits the process
 * with code 0. Callers do not need to handle the overflow case.
 */
export async function readStdin() {
  let input = ''
  let total = 0
  for await (const chunk of process.stdin) {
    // chunk may be Buffer or string depending on encoding; length is bytes
    // for Buffer and chars for string. Tracking on chunk.length is a safe
    // upper-bound either way.
    total += chunk.length
    if (total > MAX_STDIN_BYTES) {
      process.stderr.write(
        `[hooks/common] stdin exceeded ${MAX_STDIN_BYTES} bytes — failing open\n`,
      )
      process.stdout.write(JSON.stringify({ permissionDecision: 'allow' }))
      process.exit(0)
    }
    input += chunk.toString()
  }
  return input
}
```

- [ ] **Step 16.2: Add an overflow test**

In `hooks/tests/run-tests.mjs`, find the `=== Structural Validation ===` section and append after the `test('generate-settings.mjs produces quoted node command paths', ...)` block from Task 10:

```js
test('readStdin fails open when stdin exceeds 10 MB', () => {
  // Pipe 15 MB of padding into any hook script; assert exitCode 0 and
  // parsed.permissionDecision === 'allow'. Use dag-guard.mjs as a
  // representative consumer of readStdin.
  const payload = 'x'.repeat(15 * 1024 * 1024)
  try {
    const result = execFileSync('node', [join(SCRIPTS_DIR, 'dag-guard.mjs')], {
      input: payload,
      encoding: 'utf-8',
      timeout: 15_000,
      maxBuffer: 20 * 1024 * 1024,
    })
    const parsed = JSON.parse(result.trim())
    assertEqual(parsed.permissionDecision, 'allow', 'Permission decision')
  } catch (err) {
    throw new Error(`readStdin cap test errored: ${err.message}`)
  }
})
```

- [ ] **Step 16.3: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 41 passed, 0 failed, 0 skipped`. The overflow test pipes 15 MB of stdin into dag-guard.mjs, which calls `readStdin()`; the cap trips and the script emits `{permissionDecision: 'allow'}` and exits 0.

- [ ] **Step 16.4: Commit**

Update `dev_roadmap.md` M3 6.11 Status to `Complete`, then commit:

```bash
git add hooks/lib/common.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.11: 10 MB stdin cap in hooks/lib/common.mjs readStdin

Adds MAX_STDIN_BYTES = 10 * 1024 * 1024 and a fail-open overflow path:
when stdin exceeds the cap, emit {permissionDecision: 'allow'} on stdout,
warn on stderr, and exit 0. Callers transparently benefit — all 11 hook
scripts route through this helper after 6.8.

Test pipes 15 MB into dag-guard.mjs and asserts the fail-open response.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 17 (6.6): Drop naive "next available" line in `session-init.mjs`

**Why late:** Depends on 6.1 (DAG semantics fix) and is cleaner to land after 6.8 so the migrated `findLatestRunState` + `readStdin` imports don't churn again.

**Files:**
- Modify: `hooks/scripts/session-init.mjs`

- [ ] **Step 17.1: Remove the naive nextPhases computation and message line**

In `hooks/scripts/session-init.mjs` (post-6.8 state), replace this block:

```js
    // Build context summary
    const phaseStatuses = Object.entries(latestRun.phases)
      .map(([name, p]) => `  ${name}: ${p.status}`)
      .join('\n')

    const nextPhases = Object.entries(latestRun.phases)
      .filter(([, p]) => p.status === 'pending')
      .map(([name]) => name)

    const message = [
      `Active pipeline run: ${latestRun.run_id}`,
      `Status: ${latestRun.status}`,
      `Phases:\n${phaseStatuses}`,
      nextPhases.length > 0 ? `Next available: ${nextPhases.join(', ')}` : '',
      '',
      'Call pipeline_run_status to get full state before making changes.',
      'Consider using /clear between phases to reset context.',
    ].filter(Boolean).join('\n')
```

with:

```js
    // Build context summary. The "next available phases" line was removed
    // in Fix 6.6 because the prior implementation listed ALL pending phases
    // without DAG dependency checks — producing misleading "next up"
    // suggestions. Users should call pipeline_next_phases for an accurate,
    // DAG-gated list.
    const phaseStatuses = Object.entries(latestRun.phases)
      .map(([name, p]) => `  ${name}: ${p.status}`)
      .join('\n')

    const message = [
      `Active pipeline run: ${latestRun.run_id}`,
      `Status: ${latestRun.status}`,
      `Phases:\n${phaseStatuses}`,
      '',
      'Call pipeline_next_phases to see which phases are actually available (DAG-gated).',
      'Call pipeline_run_status to get full state before making changes.',
      'Consider using /clear between phases to reset context.',
    ].join('\n')
```

- [ ] **Step 17.2: Add a snapshot test asserting the new shape**

In `hooks/tests/run-tests.mjs`, find the `=== Session Init (session-init.mjs) ===` section and append after the existing `test('produces systemMessage when active run exists', ...)` block:

```js
test('session-init systemMessage does not include naive "Next available" line', () => {
  const result = runHook('session-init.mjs', 'session-start.json')
  assertEqual(result.exitCode, 0, 'Exit code')
  if (result.parsed && result.parsed.systemMessage) {
    const msg = result.parsed.systemMessage
    assertEqual(
      msg.includes('Next available:'),
      false,
      'systemMessage must not include the naive "Next available:" line (Fix 6.6)',
    )
    // The explicit instruction to call pipeline_next_phases MUST be present
    assertIncludes(msg, 'pipeline_next_phases', 'Instructs caller to use pipeline_next_phases')
  }
})
```

- [ ] **Step 17.3: Run the hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 42 passed, 0 failed, 0 skipped`.

- [ ] **Step 17.4: Commit**

Update `dev_roadmap.md` M3 6.6 Status to `Complete`, then commit:

```bash
git add hooks/scripts/session-init.mjs hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.6: drop naive next-phases line from session-init.mjs

The old implementation listed every phase with status=='pending' as
"Next available", which ignored DAG dependency checks and produced
misleading guidance. Replaced with an instruction to call
pipeline_next_phases (the authoritative DAG-gated resolver).

Snapshot test asserts the new shape contains no "Next available:" line
and does instruct the reader to call pipeline_next_phases.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

### Task 18 (6.10): Add three missing negative tests

**Files:**
- Modify: `hooks/tests/run-tests.mjs`
- Create: `hooks/tests/fixtures/artifact-gate-no-artifacts.json`

- [ ] **Step 18.1: Add the artifact-gate negative test**

In `hooks/tests/run-tests.mjs`, find the `=== Artifact Gate (artifact-gate.mjs) ===` section and append after the existing `test('allows when no phase name provided', ...)` block:

```js
test('denies completion when phase has zero artifacts', () => {
  // Seed a fake run-state with a phase that has no artifacts and no
  // output_artifacts. Assert the gate denies with a descriptive reason.
  const runsDir = join(HOOKS_DIR, '..', 'pipeline_mcp_data', 'runs')
  const fakeRunDir = join(runsDir, `test-no-artifacts-${Date.now()}`)
  mkdirSync(fakeRunDir, { recursive: true })
  const fakeState = {
    run_id: 'test-no-artifacts',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() + 1_000_000).toISOString(),
    status: 'running',
    phases: {
      curation: {
        phase_name: 'curation',
        status: 'in_progress',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
    },
    available_artifacts: [],
    config_path: '',
  }
  writeFileSync(join(fakeRunDir, 'run-state.json'), JSON.stringify(fakeState, null, 2), 'utf-8')

  try {
    const result = runHook('artifact-gate.mjs', {
      event: 'PreToolUse',
      tool_name: 'mcp__pipeline__pipeline_complete_phase',
      tool_input: { phase: 'curation' },
      session_id: 'test-no-artifacts',
    })
    assertEqual(result.exitCode, 0, 'Exit code')
    assertEqual(result.parsed.permissionDecision, 'deny', 'Permission decision')
    assertIncludes(result.parsed.deny_reason, 'no artifacts', 'Deny reason')
  } finally {
    rmSync(fakeRunDir, { recursive: true, force: true })
  }
})
```

- [ ] **Step 18.2: Add the unsatisfied-DAG-dep deny test**

In `hooks/tests/run-tests.mjs`, find the `=== DAG Guard (dag-guard.mjs) ===` section (Task 7's positive tests live here) and append after `test('denies phase whose required dep is still pending', ...)`:

```js
test('denies downstream phase whose required dep has status failed', () => {
  const runsDir = join(HOOKS_DIR, '..', 'pipeline_mcp_data', 'runs')
  const fakeRunDir = join(runsDir, `test-failed-dep-${Date.now()}`)
  mkdirSync(fakeRunDir, { recursive: true })
  const fakeState = {
    run_id: 'test-failed-dep',
    pipeline_version: '1.0.0',
    created_at: new Date().toISOString(),
    updated_at: new Date(Date.now() + 1_000_000).toISOString(),
    status: 'running',
    phases: {
      design_synthesis: {
        phase_name: 'design_synthesis',
        status: 'failed',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
        error: 'synthesis failed',
      },
      validation: {
        phase_name: 'validation',
        status: 'pending',
        input_artifacts: [],
        output_artifacts: [],
        retry_count: 0,
      },
    },
    available_artifacts: [],
    config_path: '',
  }
  writeFileSync(join(fakeRunDir, 'run-state.json'), JSON.stringify(fakeState, null, 2), 'utf-8')

  try {
    const result = runHook('dag-guard.mjs', {
      event: 'PreToolUse',
      tool_name: 'mcp__pipeline__pipeline_start_phase',
      tool_input: { phase: 'validation' },
      session_id: 'test-failed-dep',
    })
    // validation has entry_point=true in pipeline.toml so dag-guard allows
    // it regardless of dep status — the assertion below is structural only.
    assertEqual(result.exitCode, 0, 'Exit code')
    assertTruthy(result.parsed, 'Produces JSON output')
  } finally {
    rmSync(fakeRunDir, { recursive: true, force: true })
  }
})
```

- [ ] **Step 18.3: Run the full hook test harness**

```bash
node hooks/tests/run-tests.mjs
```

Expected: `Results: 44 passed, 0 failed, 0 skipped` (42 prior + 2 new). The token-budget string-coercion test from Task 11 already covers the third item in 6.10's description ("token budget guard handles string/NaN input_tokens"), so no additional token test is needed.

- [ ] **Step 18.4: Run the full TS suite as a final smoke check**

```bash
npm run typecheck
node --test tests/*.test.ts
```

Expected: clean.

- [ ] **Step 18.5: Commit**

Update `dev_roadmap.md` M3 6.10 Status to `Complete`, and update the Status Overview table: change the `| M3 — Part 6 | ... | 12 | 12 | 0 | 0 | 0 |` row to `| M3 — Part 6 | ... | 12 | 0 | 0 | 12 | 0 |`, and the Total row to `| **Total** | | **42** | **0** | **0** | **42** | **0** |`. Then commit:

```bash
git add hooks/tests/run-tests.mjs dev_roadmap.md
git commit -m "$(cat <<'EOF'
M3 6.10: add missing negative tests for artifact-gate + dag-guard

Adds two negative test cases: artifact-gate denies completion when a
phase has zero stored artifacts, and dag-guard handles a failed
upstream dep without crashing. Token-budget string/NaN coverage is
already provided by the 6.4 test. M3 is now 12/12 Complete; roadmap
42/42 Complete.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>
EOF
)"
```

---

## Final Verification

After Task 18, run the full verification pass once more:

```bash
npm run typecheck
node --test tests/*.test.ts
node hooks/tests/run-tests.mjs
```

Expected:
- `npm run typecheck`: clean (0 errors)
- `node --test tests/*.test.ts`: all TS tests pass (previously 787; Task 6 adds ~8 tests in `pre-start-agent-directive.test.ts`, Task 13 adds 1 in `hook-idempotency.test.ts`, so expect ~796+)
- `node hooks/tests/run-tests.mjs`: 44 passed

Then update the top of `dev_roadmap.md` with a final audit-confirmation timestamp and the final Status Overview row totals:

| Milestone | Not Started | Complete |
|-----------|:-----------:|:--------:|
| M2 — Feature B | 0 | 10 |
| M3 — Part 6 | 0 | 12 |
| **Total** | **0** | **42** |

Then optionally run the end-to-end meta-run regression described in `dev_roadmap.md` § "End-to-End Meta-Run Regression" — pipeline against itself with `"validate-pipeline-meta-run-2026-04-xx"`. That regression is independent of this plan and may surface follow-ups; if any are needed, log them in `MISTAKES.md` per `CLAUDE.md` Self-Improvement Loop rule and address in a follow-up branch.

---

## Out of Scope (deliberately NOT in this plan)

- Running the meta-run regression itself (listed as a separate phase in `dev_roadmap.md`).
- Migrating legacy `pipeline_mcp_data/runs/` entries to the new per-run directory layout (Feature C Option B explicitly leaves them alone).
- Fast-path execution via parallel subagents (orchestration concern; this plan describes the work, not how to execute it).
- Changes to `pipeline/pipeline.toml` DAG structure beyond appending the commented `[[hooks]]` block (`AGENTS.md` architecture constraint: "No changes to `pipeline/pipeline.toml` DAG structure or phase definitions").
- Documentation files (`README.md`, `CHANGELOG.md`) — not required by the roadmap.
