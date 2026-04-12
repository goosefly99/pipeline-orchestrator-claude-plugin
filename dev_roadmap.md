# Pipeline Orchestrator — Development Roadmap

> **Scope:** Complete the PROPOSED work from the scaffolded implementation plan at
> `pipeline_mcp_data/scaffold/IMPLEMENTATION-PLAN.md`.
>
> **Branch:** `auto_dev` (all work lands here; do not create new branches).
>
> **Source spec:** `pipeline_mcp_data/specs/fix-artifact-type-mismatch-validated-spec.json`
> **Meta-run ID:** `pipeline-meta-test-2026-04-10`
>
> **Rule:** Apply items in order. Each item is independently commitable. Run
> `npm test && npm run typecheck` after every item and only commit when both are clean.
> Update the **Status** column in the same commit that lands the change.

---

## Status Overview

| Milestone | Scope | Items | Not Started | In Progress | Complete | Blocked |
|-----------|-------|-------|:-----------:|:-----------:|:--------:|:-------:|
| M0 — Part 1 & Part 2 & Feature A | Quality-gates fix + 7 post-meta-run fixes + pre-init hook | 10 | 0 | 0 | 10 | 0 |
| M1 — Feature C | Per-run hierarchical artifact directories | 10 | 0 | 0 | 10 | 0 |
| M2 — Feature B | Pre-phase-start hook with Agent-per-phase execution | 10 | 0 | 0 | 10 | 0 |
| M3 — Part 6 | Hooks code-review fixes (harness runtime) | 12 | 12 | 0 | 0 | 0 |
| **Total** | | **42** | **12** | **0** | **30** | **0** |

**Current work focus:** M1 → M2 → M3 (M3 can execute in parallel with M1/M2 since it
targets `hooks/scripts/` rather than the TypeScript MCP server).

### Audit Confirmation (2026-04-11)

Full codebase review against this roadmap confirmed all status claims in the table
above are **accurate**. No silent drift detected. Current test health on `auto_dev`:

| Check | Result |
|-------|--------|
| `npm run typecheck` | clean (0 errors) |
| `node --test tests/*.test.ts` | **787/787** passing (174 suites, 1232ms) |
| `node hooks/tests/run-tests.mjs` | **30/30** passing |

Verified-complete items spot-checked with file/line evidence:

- **M1 C1** → `storage.ts:50-110` (helpers, `ArtifactSubtype` union at 23-29)
- **M1 C2** → `run-state.ts:51-101` (initRun computes `run_data_dir`), `types.ts:87`
- **M1 C8** → `run-state.ts:37-49` (`resolveRunWriteDir`)
- **M1 C9** → `tests/per-run-paths.test.ts` (collision + legacy-fallback coverage)
- **M1 C10** → `AGENTS.md:108-163` (`## Per-Run Artifact Directories` section)
- **M2 B1** → `types.ts:219-234` (`AgentDirective`), `types.ts:36` (`Phase.model`)
- **M2 B2** → `hooks.ts:39-56` (`HookResult.agent_directive`), `hooks.ts:73-106` (`parseAgentDirective`)
- **M2 B3** → `toml-loader.ts:31` (`model: raw.model as string | undefined`)
- **M2 B4** → `pipeline/pipeline.toml:16-32` (precedence comment block), per-phase `model` on all 9 phases

Verified-absent items confirmed by file absence or grep:

- **M2 B5-B10** → `handleStartPhase` ignores hook-return value, no `agent_directive` in response; `generatePhaseBrief` signature unchanged; no `hooks/pre-phase-start.example.js`; no `[[hooks]]` block in `pipeline/pipeline.toml`; no `### Phase Execution via Subagent` in `AGENTS.md`; no `tests/pre-start-agent-directive.test.ts`.
- **M3 6.1-6.12** → all 12 items still not started. See **M3 Audit Evidence (2026-04-11)** below for precise line references discovered during the audit — copy these into the first implementation commit for each item.

---

## Dependency Graph

```
  M0 (DONE)
   │
   ├─ Fix 3.1  ──┐
   ├─ Fix 3.2  ──┤
   ├─ Fix 3.3  ──┤
   ├─ Fix 3.4  ──┤
   ├─ Fix 3.5  ──┤
   ├─ Fix 3.6  ──┤
   ├─ Fix 3.7  ──┤
   └─ Feat. A  ──┼──► M1 Feature C ──► M2 Feature B
                 │
                 └──► M3 Part 6 (orthogonal, any time)
```

Feature C needs Feature A (`run_parameters.run_name`, `run_parameters.run_directory_timestamp`).
Feature B needs Features A and C (reads `run_parameters.phase_model`, injects
`run_data_dir` into phase brief). Part 6 touches the Claude Code harness hook runtime
under `hooks/scripts/` and is independent of the TypeScript server.

---

## M0 — Completed Work (reference)

These items are APPLIED on `auto_dev` (commit `2417862` and prior). Do NOT re-apply.

| ID | Description | Files | Status |
|----|-------------|-------|:------:|
| 1.1 | quality-gates.toml: `concept-overview` → `knowledge-overview` | `pipeline/quality-gates.toml` | Complete |
| 1.2 | Config-integrity test with `KNOWN_ARTIFACT_TYPES` | `tests/quality-gates-config.test.ts` | Complete |
| 3.1 | Terminal status detection after complete_phase | `run-state.ts`, `lifecycle-handlers.ts`, `tests/run-state-terminal.test.ts` | Complete |
| 3.2 | Hook idempotency (TS layer; `post-phase-complete.mjs` still in 6.5) | `hooks.ts`, `lifecycle-handlers.ts`, `tests/hook-idempotency.test.ts` | Complete |
| 3.3 | Scaffold artifact registration + `pipeline_register_scaffold_outputs` | `misc-handlers.ts`, `server.ts`, `tool-schemas.ts`, `pipeline/schemas/scaffold-document.json`, `pipeline/schemas/scaffold-manifest.json`, `tests/scaffold-register-outputs.test.ts` | Complete |
| 3.4 | `input_artifacts` populated from DAG at start_phase | `run-state.ts`, `lifecycle-handlers.ts`, `phase-brief.ts`, `handoff.ts`, `tests/input-artifact-resolution.test.ts` | Complete |
| 3.5 | AGENTS.md.proposed-diff anchor validation + `## Quality Gates` section | `AGENTS.md`, `pipeline_mcp_data/scaffold/AGENTS.md.proposed-diff.md`, `misc-handlers.ts`, `tests/scaffold-anchor-validation.test.ts` | Complete |
| 3.6 | `gate_evaluated` + `artifact_stored` events | `types.ts`, `lifecycle-handlers.ts`, `artifact-handlers.ts`, `tests/quality-gates.test.ts`, `tests/artifact-handlers.test.ts` | Complete |
| 3.7 | `validation-report` schema + completeness flip | `pipeline/schemas/validation-report.json`, `pipeline/pipeline.toml`, `misc-handlers.ts`, `tests/validation-report-schema.test.ts`, `tests/cross-ref-validator.test.ts` | Complete |
| A | pre-pipeline-init parameterization hook | `types.ts`, `hooks.ts`, `toml-loader.ts`, `lifecycle-handlers.ts`, `server.ts`, `tool-schemas.ts`, `pipeline/pipeline.toml`, `hooks/pre-pipeline-init.example.js`, `tests/pre-init-hook.test.ts` | Complete |

---

## M1 — Feature C: Per-Run Hierarchical Artifact Directory Structure

**Goal:** Move artifact storage from per-type top-level folders
(`pipeline_mcp_data/collections/`, `debates/`, `overviews/`, …) to per-run trees rooted
at `pipeline_mcp_data/runs/{sanitized_run_name}-{sanitized_timestamp}/`. Prevents newer
runs from clobbering older artifacts and enables clean cross-run diffs.

**Inputs consumed:** `run_parameters.run_name` and `run_parameters.run_directory_timestamp`
persisted by Feature A (M0).

**Backwards compatibility strategy:** Option B from the plan. Leave existing runs in
place. New runs use the new layout. `pipeline_list_artifacts` falls back to legacy
top-level paths when `state.run_data_dir` is absent (pre-Feature-C runs).

### Tasks

| ID | Description | Target Files | Depends On | Status |
|----|-------------|--------------|:----------:|:------:|
| C1 | Add path helpers: `sanitizeRunName`, `sanitizeRunTimestamp`, `getRunDataDir`, `getArtifactDir`. Define `ArtifactSubtype` union (`'collections/curated' \| 'collections/raw' \| 'debates' \| 'overviews' \| 'specs' \| 'scaffold'`). Sanitization: `/[^a-zA-Z0-9-]/g → '-'`, lowercase, trim trailing hyphens, truncate 64 chars; timestamp: ISO with `:` and `.` → `-`. | `storage.ts`, `tests/storage.test.ts` | — | Complete |
| C2 | In `initRun`, compute `state.run_data_dir` from `run_parameters.run_name` + `run_parameters.run_directory_timestamp`. Fall back to legacy `runs/{run_id}/` when parameters are absent and emit a warning. Persist into run-state. Add `run_data_dir?: string` to `RunState` in `types.ts`. | `run-state.ts`, `types.ts` | C1 | Complete |
| C3 | Route curated/raw collection writes through `getArtifactDir(state.run_data_dir, 'collections/curated' \| 'collections/raw')`. | `collections.ts`, `misc-handlers.ts` | C1, C2 | Complete |
| C4 | Route concept overview writes through `getArtifactDir(state.run_data_dir, 'overviews')`. Update both the embedded and standalone code paths. | `concepts.ts`, `cc-handlers.ts` | C1, C2 | Complete |
| C5 | Route debate transcript writes through `getArtifactDir(state.run_data_dir, 'debates')`. | `debate-handlers.ts`, `debate.ts` | C1, C2 | Complete |
| C6 | Route synth spec writes through `getArtifactDir(state.run_data_dir, 'specs')`. | `synth-handlers.ts`, `synth.ts` | C1, C2 | Complete |
| C7 | Route scaffold-document writes through `getArtifactDir(state.run_data_dir, 'scaffold')`. Update `handleRegisterScaffoldOutputs` to accept run-scoped base dir. Update `artifact-handlers.ts` (`pipeline_store_artifact`, `pipeline_load_artifact`, `pipeline_list_artifacts`) to resolve paths via the run-scoped helpers for new runs. | `misc-handlers.ts`, `artifact-handlers.ts` | C1, C2 | Complete |
| C8 | Write `run-state.json` and `events.jsonl` under `state.run_data_dir` directly (no extra subfolder). Implement legacy fallback in `pipeline_list_artifacts`: if `state.run_data_dir` is absent, resolve from `pipeline_mcp_data/<subtype>/` top-level. | `run-state.ts`, `artifact-handlers.ts` | C7 | Complete |
| C9 | New integration test `tests/per-run-paths.test.ts`: (a) unit tests on `sanitizeRunName` incl. Windows-unsafe chars and Unicode, (b) two-run sequence asserting zero path collisions under `pipeline_mcp_data/runs/{name-timestamp}/`, (c) legacy-fallback test seeding a pre-Feature-C run-state and asserting `pipeline_list_artifacts` still resolves legacy top-level paths. | `tests/per-run-paths.test.ts` (new) | C1–C8 | Complete |
| C10 | Grep tests dir for hardcoded `pipeline_mcp_data/<subtype>/` literals and migrate them to the helpers. Add migration note to `AGENTS.md` under a new `## Per-Run Artifact Directories` subsection explaining cutover semantics. | `tests/**/*.test.ts`, `AGENTS.md` | C1–C9 | Complete |

### Verification (M1)

1. `npm run typecheck` — zero errors.
2. `node --test tests/*.test.ts` — all tests pass, including the new `tests/per-run-paths.test.ts`.
3. Manual sanity: initialize a run, confirm artifacts land under `pipeline_mcp_data/runs/{name-timestamp}/` and `run-state.json` / `events.jsonl` sit at that dir's root.
4. Legacy sanity: read a pre-Feature-C run via `pipeline_list_artifacts` and confirm fallback path resolution still returns artifacts.

---

## M2 — Feature B: Pre-Phase-Start Hook with Agent-Per-Phase Execution

**Goal:** Each pipeline phase is executed by a freshly-spawned Agent subagent invoked
with the model resolved via precedence: phase-specific `model` in `pipeline.toml` >
`run_parameters.phase_model` > hard-coded `claude-sonnet-4-6` default. This isolates
per-phase context (addresses the 83.9% extra-usage / quadratic context-growth overhead
documented in `pipeline_mcp_data/pipeline_design_inefficiencies_assessment.txt`), enables
cost-tiering, and prevents context bleed between phases.

**Orchestration contract:** When `pipeline_start_phase` returns an `agent_directive`,
the client MUST spawn an Agent subagent with those exact parameters. The subagent calls
`pipeline_complete_phase` (or `pipeline_fail_phase`) on finish. The parent session does
not wait synchronously — subsequent `pipeline_next_phases` calls observe the state
written by the subagent.

### Tasks

| ID | Description | Target Files | Depends On | Status |
|----|-------------|--------------|:----------:|:------:|
| B1 | Add `AgentDirective` interface (`subagent_type`, `model`, `description`, `prompt`, `isolation?`). Add optional `model?: string` to the `Phase` type. | `types.ts` | — | Complete |
| B2 | Extend `HookResult` with optional `agent_directive?: AgentDirective`. Parse `agent_directive` from hook stdout JSON inside the existing hook runner. | `hooks.ts` | B1 | Complete |
| B3 | Extend `loadPhaseConfig` / phases parser in `toml-loader.ts` to read optional `model` field per phase. Maintain backwards compatibility (undefined = fall through precedence). | `toml-loader.ts` | B1 | Complete |
| B4 | Add per-phase model overrides to `pipeline/pipeline.toml` — e.g. `debate = "claude-opus-4-6"`, planning-heavy phases use Opus, bounded execution phases use Sonnet. Document convention in a comment block above `[[phases]]`. | `pipeline/pipeline.toml` | B3 | Complete |
| B5 | In `handleStartPhase` (lifecycle-handlers.ts): after `generatePhaseBrief` but before returning, (a) resolve `phase_model` via precedence, (b) call `runHooks` with `pre_start` trigger passing `phase`, `run_parameters`, `phase_brief`, resolved `model`, (c) collect any returned `agent_directive`, (d) include it in the response JSON. | `lifecycle-handlers.ts` | B2, B3, M1-C2 | Complete |
| B6 | Update `generatePhaseBrief` signature to accept resolved `model` and `run_data_dir`. Include both in the rendered brief text so the spawned subagent sees its model identity and the run's data root. | `phase-brief.ts` | M1-C2 | Complete |
| B7 | Create example hook `hooks/pre-phase-start.example.js`: reads `PIPELINE_HOOK_CONTEXT`, computes resolved model, emits `{ agent_directive: { subagent_type: "general-purpose", model, description: "<phase> execution", prompt: <brief + completion instruction + no-context-inheritance rule> } }` to stdout. | `hooks/pre-phase-start.example.js` (new) | B5 | Complete |
| B8 | Register the example hook (disabled-by-default, commented out) under `[[hooks]]` in `pipeline/pipeline.toml` with `trigger = "pre_start"`, `phase_filter = "*"`. Users opt in by uncommenting. | `pipeline/pipeline.toml` | B7 | Complete |
| B9 | Add `### Phase Execution via Subagent` subsection under the `## Quality Gates` block in `AGENTS.md` documenting the client-side contract (spawn Agent on `agent_directive`, no sync wait, subagent owns `complete_phase`). | `AGENTS.md` | B5 | Complete |
| B10 | New test `tests/pre-start-agent-directive.test.ts`: (a) unit — hook returns expected directive per phase with precedence-resolved model, (b) handler — `handleStartPhase` includes `agent_directive` when hook returns one and omits it otherwise, (c) integration — mock 9-phase run asserts each `start_phase` returns the expected model. | `tests/pre-start-agent-directive.test.ts` (new) | B1–B9 | Complete |

### Verification (M2)

1. `npm run typecheck` — zero errors.
2. `node --test tests/*.test.ts` — all tests pass including `tests/pre-start-agent-directive.test.ts`.
3. Run a real pipeline against a trivial spec; visually confirm each phase spawns an Agent subagent and the subagent calls `pipeline_complete_phase` on finish.
4. Cost check: after running a full pipeline, compare total tokens to the baseline in `pipeline_design_inefficiencies_assessment.txt`. Target: sub-agent extra-usage rate drops from 83.9% toward 67%.

---

## M3 — Part 6: Hooks Code-Review Fixes

**Scope:** `hooks/scripts/` and `hooks/generate-settings.mjs` — the Claude Code harness
hook runtime. This is a separate Node runtime triggered by PreToolUse/PostToolUse/Stop/SessionStart
events, independent of the TypeScript MCP server. Part 6 can land in parallel with M1/M2.

**Severity summary:** 2 critical correctness bugs (6.1), 1 high-severity security (6.2),
3 medium-security (6.3, 6.4, 6.7), 6 maintainability/quality.

**Pre-flight rule for every item:** Inspect the target file AS IT CURRENTLY EXISTS on
`auto_dev` before patching. The audit line numbers were captured before subsequent edits.
If a bug is already fixed, flip the item to `Complete` with a one-line note citing the
commit; do not re-apply landed fixes.

### Tasks

| ID | Severity | Description | Target Files | Depends On | Status |
|----|:--------:|-------------|--------------|:----------:|:------:|
| 6.1 | **CRITICAL** | DAG semantics divergence from canonical `dag.ts`. Replace `every(dep => phase.status !== 'completed')` with `some(...)` pattern accepting both `'completed'` AND `'skipped'` as dependency-satisfying statuses. Duplicate correct logic in both files until 6.8 lands; add TODO cross-ref. New negative test in `hooks/tests/run-tests.mjs` seeding a mixed completed+skipped fixture. | `hooks/scripts/dag-guard.mjs`, `hooks/scripts/post-phase-complete.mjs`, `hooks/tests/run-tests.mjs` | — | Complete |
| 6.2 | **HIGH SEC** | Path traversal via unsanitized `session_id`. Apply `sessionId.replace(/[^a-zA-Z0-9_-]/g, '_')` and reject sanitized IDs > 128 chars (fail-closed). Unit test with `../../../etc/passwd`, normal IDs, and 200-char inputs. | `hooks/scripts/phase-start-guard.mjs`, `hooks/tests/run-tests.mjs` | — | Complete |
| 6.3 | MED SEC | Unquoted `node <path>` command silently disables hooks on paths with spaces. Wrap resolved script path in double quotes in the generated settings JSON; escape embedded quotes defensively. Regression test with a tempdir whose path contains a space. | `hooks/generate-settings.mjs`, `hooks/tests/run-tests.mjs` | — | Complete |
| 6.4 | MED SEC | NaN bypass in token budget guard. Coerce each of `input_tokens`, `output_tokens`, `cache_creation_input_tokens`, `cache_read_input_tokens` via `const n = Number(x); return isFinite(n) ? n : 0`. Defense-in-depth: non-finite `totalCost` → treat as budget-exceeded. Negative test with `"input_tokens": "99999"`. | `hooks/scripts/token-budget-guard.mjs`, `hooks/tests/run-tests.mjs` | — | Complete |
| 6.5 | CORRECTNESS | Duplicate `phase_completed` event write — follow-up to Fix 3.2. Delete the `appendFileSync(eventsPath, ...)` block in `post-phase-complete.mjs` (verify current line numbers). Preserve the `systemMessage` "next phases" emission. Extend `tests/hook-idempotency.test.ts` (or new integration test) to assert exactly ONE `phase_completed` per phase after a full pipeline run. | `hooks/scripts/post-phase-complete.mjs`, `tests/hook-idempotency.test.ts` | — | Complete |
| 6.6 | QUALITY | Naive next-phases calculation in `session-init.mjs` lists ALL pending phases as "next available" without DAG dependency checks. Recommended: Option B — drop the "Next available" line entirely and instruct the user to call `pipeline_next_phases`. Snapshot test of output. | `hooks/scripts/session-init.mjs`, `hooks/tests/run-tests.mjs` | 6.1 | Not Started |
| 6.7 | MED SEC | Path normalization bypass in file-read version guard. `import { normalize } from 'node:path'`; normalize `file_path` before any substring checks. Fail-closed on any remaining `..` segment. Unit test with crafted bypass paths. | `hooks/scripts/file-read-version-guard.mjs`, `hooks/tests/run-tests.mjs` | — | Complete |
| 6.8 | DEDUPE | Create `hooks/lib/common.mjs` exporting `readStdin()`, `loadConfig(configPath)`, `findLatestRunState(runsDir)`. Migrate all 11 scripts in `hooks/scripts/` to import from the shared module. Accommodate `post-phase-complete.mjs`'s `{state, dir}` variant via a second function or `options: { includeDir?: boolean }`. Verify Claude Code hook runtime resolves the relative import in a fresh Node process. | `hooks/lib/common.mjs` (new), `hooks/scripts/*.mjs` (11 files) | — | Complete |
| 6.9 | CLEANUP | Delete dead `CONFIG_PATH` constant at line ~11 of `dag-guard.mjs`. Trivial one-line cleanup. | `hooks/scripts/dag-guard.mjs` | 6.8 | Complete |
| 6.10 | TESTING | Add three missing negative test cases: (a) DAG guard denies unsatisfied dependency, (b) artifact gate denies when no artifacts exist, (c) token budget guard handles string/NaN `input_tokens`. Add supporting fixtures under `hooks/tests/fixtures/`. | `hooks/tests/run-tests.mjs`, `hooks/tests/fixtures/` | 6.1, 6.4 | Not Started |
| 6.11 | HARDENING | Add 10 MB stdin size cap in `readStdin()`. On cap exceeded: emit `{ permissionDecision: 'allow' }` and `process.exit(0)` (fail-open), write warning to stderr. Unit test with 15 MB input. | `hooks/lib/common.mjs` | 6.8 | Complete |
| 6.12 | CLEANUP | Session tracking file TTL cleanup. Before writing the current session's file, scan `$TMPDIR/pipeline-hooks-sessions/` and remove files with `mtime < Date.now() - 24h`. Ignore unlink errors. Cap cleanup work at 100 files per invocation. Unit test seeding 3 files (2 stale, 1 fresh). | `hooks/scripts/phase-start-guard.mjs` | 6.2 | Complete |

### M3 Audit Evidence (2026-04-11)

Confirmed line numbers on `auto_dev` at audit time. Use these as the first anchor for
each fix commit — re-read the target file before editing and update anchors if prior
fixes in the same file have shifted lines.

| ID | Target | Current-state evidence (line numbers verified) |
|----|--------|-------------------------------------------------|
| 6.1 | `hooks/scripts/dag-guard.mjs:93-96` | `every(dep => phase.status !== 'completed')` — **rejects `skipped` deps.** Canonical is `dag.ts:59` (`completed.has(...) \|\| skipped.has(...)`). |
| 6.1 | `hooks/scripts/post-phase-complete.mjs:70-73` | Same bug: `every(dep => depState?.status === 'completed')` only accepts `'completed'`, skips `'skipped'`. Both hook files diverge from canonical `dag.ts:59` in the same way. |
| 6.2 | `hooks/scripts/phase-start-guard.mjs:28, 36` | `const sessionId = hookInput.session_id \|\| 'unknown'` (unsanitized); used directly in `` join(trackDir, `session-${sessionId}.json`) `` — enables `../` path traversal. |
| 6.3 | `hooks/generate-settings.mjs:58` | `` command: `node ${join(scriptsDir, scriptFile).replace(/\\/g, '/')}` `` — **unquoted**. Paths with spaces break hook invocation silently. |
| 6.4 | `hooks/scripts/token-budget-guard.mjs:40-43, ~96` | Token counts read directly from `usage.{input,output,cache_creation_input,cache_read_input}_tokens` with no `Number()`/`isFinite()` guard; `totalCost` not checked for finiteness. |
| 6.5 | `hooks/scripts/post-phase-complete.mjs:98-109` | `appendFileSync(eventsPath, JSON.stringify(event) + '\n', ...)` still writes `phase_completed` — **duplicate of TS-layer Fix 3.2**. Lines 114-126 `systemMessage` emission must be preserved when this block is deleted. |
| 6.6 | `hooks/scripts/session-init.mjs` | `nextPhases` line lists all `status === 'pending'` phases without DAG dep check. Option B: drop the "Next available" line. |
| 6.7 | `hooks/scripts/file-read-version-guard.mjs:66` | `normalizedPath = filePath.toLowerCase().replace(/\\/g, '/')` — only case+slash normalization. No `path.normalize()`, no `..` rejection. |
| 6.8 | `hooks/lib/` | **Directory does not exist.** All 11 scripts reimplement `for await (const chunk of process.stdin)` stdin reader. `findLatestRunState()` is duplicated in `dag-guard.mjs:15-41`, `post-phase-complete.mjs:14-42`, `artifact-gate.mjs`, `stop-guard.mjs`. Config-load try/catch duplicated in ~10 scripts. |
| 6.9 | `hooks/scripts/dag-guard.mjs:11` | `const CONFIG_PATH = join(__dirname, '..', 'runtime-config.json')` — **declared but never referenced** in the file. Trivial one-line delete (but easier post-6.8). |
| 6.10 | `hooks/tests/run-tests.mjs` | No negative test for: (a) DAG guard deny on unsatisfied dep, (b) artifact gate deny on empty artifacts, (c) token budget string/NaN coercion. Fixture directory `hooks/tests/fixtures/` exists but needs three additions. |
| 6.11 | `hooks/lib/common.mjs` | Not yet created (see 6.8). When authored, `readStdin()` must cap at 10 MB and fail-open (`{permissionDecision: 'allow'}`, exit 0) on overflow. |
| 6.12 | `hooks/scripts/phase-start-guard.mjs:33-44` | No TTL cleanup of `$TMPDIR/pipeline-hooks-sessions/`. Files written indefinitely. Scope: remove mtime-age > 24h entries, cap 100 files per invocation, ignore unlink errors. |

**Behavioural note on 6.1:** The bug is worse than "divergence" — both hook files
rely only on `'completed'`, so **any pipeline run that uses skipped phases will be
mis-gated on the PreToolUse path** and will also fail to surface downstream phases
as `next` in post-complete. Prefer landing 6.1 early; it's the correctness foundation
for both the DAG guard and the post-complete systemMessage.

### Recommended intra-M3 ordering

Per the plan's ordering section (§ "Part 6 — Hooks code-review fixes — orthogonal"):

1. **6.1** first (unblocks 6.6, critical correctness).
2. **6.2** next (high-severity security; co-locate with 6.12 — same file).
3. **6.3, 6.4, 6.7** in parallel (independent medium-security).
4. **6.5** as the highest-priority follow-up to Fix 3.2.
5. **6.8** (shared module) — unblocks 6.9 and 6.11.
6. **6.9, 6.11** after 6.8.
7. **6.6** after 6.1 (and ideally after 6.8 to avoid duplication).
8. **6.10** after 6.1 and 6.4.
9. **6.12** with or after 6.2.

### Verification (M3)

1. `node hooks/tests/run-tests.mjs` — all hook tests pass including new negative cases.
2. `npm run typecheck` and `node --test tests/*.test.ts` — TypeScript server unaffected.
3. Manual: run `hooks/generate-settings.mjs` and inspect the generated JSON for quoted paths.

---

## End-to-End Meta-Run Regression (after all milestones)

After M1, M2, and M3 are all `Complete`, run the pipeline against itself with a feature
request of the form `"validate-pipeline-meta-run-2026-04-xx"` and assert:

- Every phase executed by a subagent (visible as `agent_directive` entries in the events log).
- Run artifacts land under `pipeline_mcp_data/runs/validate-pipeline-meta-run-2026-04-xx-{timestamp}/`.
- `run-state.status` transitions `running` → `done`, `completed_at` populated (Fix 3.1).
- Exactly one `phase_completed` event per phase in `events.jsonl` (Fix 3.2 + 6.5).
- `gate_evaluated` and `artifact_stored` events present in `events.jsonl` (Fix 3.6).
- All four scaffold documents registered with `artifact_type` in `{scaffold-document, scaffold-manifest}` (Fix 3.3).
- Cost profile: extra-usage overhead drops meaningfully from the 83.9% baseline toward the ~67% subagent level documented in `pipeline_design_inefficiencies_assessment.txt`.

---

## Completion Criteria

The roadmap is `Complete` when:

1. All 32 remaining items in M1, M2, M3 are marked `Complete`.
2. `npm run typecheck && node --test tests/*.test.ts` passes clean on `auto_dev`.
3. The meta-run regression above has executed end-to-end with all assertions green.
4. `AGENTS.md` has both the `### Phase Execution via Subagent` subsection (B9) and the
   Per-Run Artifact Directories migration note (C10).
5. All deferred nits from APPLIED adversarial reviews that were noted in the plan's
   status banners remain either addressed or intentionally logged as follow-ups in
   `MISTAKES.md`.

---

## Reference Documents

| File | Purpose |
|------|---------|
| `dev_roadmap.md` | This file — phase tracking and task status |
| `pipeline_mcp_data/scaffold/IMPLEMENTATION-PLAN.md` | Authoritative implementation plan with line-numbered diffs, before/after code, test specs, and adversarial-review sign-offs |
| `pipeline_mcp_data/scaffold/scaffold-manifest.json` | Scaffold manifest with status tracking per fix |
| `pipeline_mcp_data/scaffold/AGENTS.md.proposed-diff.md` | Proposed AGENTS.md patch (APPLIED for Fix 3.5) |
| `pipeline_mcp_data/specs/fix-artifact-type-mismatch-validated-spec.json` | Source design spec (v1.1.0) |
| `pipeline_mcp_data/specs/fix-artifact-type-mismatch-validation-report.json` | Validation results for the spec (VAL-1 through VAL-7) |
| `pipeline_mcp_data/debates/fix-artifact-type-mismatch-quality-gates-debate-dt-f5cb9eff.json` | Multi-agent debate synthesis |
| `pipeline_mcp_data/overviews/pipeline-mcp-server-architecture-implementation-knowledge-ov--a3f7c2e1.json` | Architectural concept overview (handler extraction, state guards, quality gates, ID cross-ref) |
| `pipeline_mcp_data/codebase/pipeline-mcp-requirements.json` | Codebase analysis snapshot — architecture constraints, file structure, dependencies |
| `pipeline_mcp_data/pipeline_design_inefficiencies_assessment.txt` | Cost analysis motivating Feature B (83.9% extra-usage baseline) |
| `AGENTS.md` | Root project conventions, architecture constraints, core patterns, Quality Gates drift gotcha |
| `CLAUDE.md` (user global) | Development rules, phased execution, context discipline, verification requirements |
| `pipeline/pipeline.toml` | DAG definition and phase config |
| `pipeline/quality-gates.toml` | Quality gate definitions |
| `pipeline/schemas/` | 12 JSON schemas for artifact validation |

---

## Build, Test, and Type-Check Commands

```bash
# From repo root (C:\Users\olive\Documents\pipeline_orchestrator_dev)
npm run typecheck          # tsc --noEmit, strict mode, zero errors expected
node --test tests/*.test.ts  # node:test runner; all tests must pass
node hooks/tests/run-tests.mjs  # hooks harness tests (for M3 work)
```

All three must be clean before committing any item. Never skip a check — pre-existing
lint/type errors in untouched files must be either fixed or explicitly flagged per the
user's global CLAUDE.md verification rule.
