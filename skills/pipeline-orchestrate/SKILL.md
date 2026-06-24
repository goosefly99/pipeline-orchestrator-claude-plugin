---
name: pipeline-orchestrate
description: Drive a pipeline-orchestrator run end-to-end. Use when starting or advancing a multi-phase pipeline run — inspecting config, initializing a run, then looping next-phases → brief → start → work → validate → store → complete through the typed-DAG until the run is done.
user-invocable: true
---

# pipeline-orchestrate — drive a run end-to-end

The front door for the `pipeline-orchestrator` plugin. Use this when you need
to **start a new pipeline run or advance an existing one** through its typed
phase DAG, from config inspection to final whole-run validation.

The plugin reads a declarative `pipeline/pipeline.toml` (the phase DAG, with
each phase's inputs, outputs, tools, and gate bindings), `pipeline/quality-gates.toml`,
and `pipeline/schemas/*.json` (one JSON schema per artifact type). A **run**
advances phases through that DAG; each phase produces artifact(s) that are
validated against schemas and stored, after which the phase is completed and
its gates are evaluated.

## When to use

- The operator asks to "run", "start", or "continue" a pipeline.
- You need to take a research/design/scaffold pipeline from one phase to the next.

For **read-only** situational checks (what phase am I in? what's next?) use the
sibling skill `pipeline-run-status`. To **carry context across a phase boundary
or a new session**, use `pipeline-handoff`.

## The loop

Derive no state from memory between calls — always re-read the tool results.

1. **`pipeline_get_config`** — inspect the phase DAG, edges, debate config,
   storage paths, and schema references before you begin.

2. **`pipeline_init_run`** — initialize a new run (all phases pending). Capture
   the returned `run_id` and starting phases; the `run_id` is needed for every
   later call and for the `output_dir` path. *(Resuming an existing run? Skip
   to step 3 and use `pipeline-run-status` / `pipeline_reload_state` to re-sync.)*

3. **`pipeline_next_phases`** — the authoritative set of phases runnable right
   now, based on completed phases, available artifacts, and DAG edges. **Always
   re-read this; never infer the next phase from memory.** Empty result → the
   run is terminal: go to step 9.

4. **`pipeline_phase_brief`** *(phase)* — a self-contained brief for the chosen
   phase: description, input artifact paths, output requirements, gate criteria,
   storage paths, and tool list. Use it to orient before doing any work.

5. **`pipeline_start_phase`** — mark the phase in-progress; returns the phase
   definition (inputs, outputs, tools).

6. **Do the phase's work**, using the phase-domain tool groups (below) as the
   brief directs, and produce the phase's artifact(s).

7. **`pipeline_validate_artifact`** — validate each JSON artifact against its
   schema (pass inline `artifact` or a `file_path`). **HARD-STOP on failure:**
   do not store or complete. Instead call **`pipeline_fail_phase`** with the
   error, surface it to the operator, and stop. Call **`pipeline_retry_phase`**
   (failed → pending) only if the operator asks to retry.

8. **Register the artifact, then complete:**
   - **`pipeline_store_artifact`** — store a validated artifact (inline or
     `file_path`) and register it in run state.
   - **`pipeline_register_artifact`** — instead, register a large (>256KB)
     file already on disk without inlining its content.
   - **`pipeline_register_scaffold_outputs`** — for a scaffold phase, scan the
     scaffold dir and register every file; call once at the end of the
     `implementation_scaffold` phase. *(Phases that declare empty `outputs`
     produce no plugin artifact — steps 7–8 are then no-ops.)*
   - **`pipeline_complete_phase`** — mark the phase completed (gates evaluate
     on completion). Then loop back to step 3.

9. **`pipeline_validate_run`** — once `pipeline_next_phases` is empty, run the
   whole-run cross-reference, semantic, and completeness validation. Surface
   the structured report to the operator.

## Phase-domain tool groups

Inside step 6, a phase's brief will point you at one or more of these. Pick the
group that matches the phase's purpose:

- **Research / ingest** — gather raw material into a collection.
  `pipeline_ingest_documents` (local files → raw-collection),
  `pipeline_web_search` (search snippets and/or fetch+merge URLs),
  `pipeline_analyze_codebase` (package dir → codebase requirements),
  `pipeline_kb_search` (knowledge-base lookup).
- **Concepts** — extract structured concepts from a research collection.
  The `pipeline_cc_*` group (load → query → collect → save/list/get overviews).
- **Synthesis** — turn research into a design spec.
  The `pipeline_synth_*` group (load → query → create-spec → save/list specs).
- **Debate** — structured multi-agent debate.
  `pipeline_init_debate` → `pipeline_submit_argument` →
  `pipeline_synthesize_debate` → `pipeline_save_debate`.
- **Capture** — `pipeline_feature_request` records improvement ideas for later
  review-spec design without interrupting the run.

When a phase tool saves an agent-direct artifact (e.g. `pipeline_synth_save_spec`,
`pipeline_cc_save_overview`), pass
`output_dir = "projects/<project>/pipeline-orchestrate/<run_id>/"` so writes
land under the predictable run path.

The full per-tool reference for each group is in `## References` below.

## References

### Lifecycle (11)

| Tool | Purpose |
|------|---------|
| `pipeline_get_config` | Get phases, edges, debate config, storage paths, schema refs. |
| `pipeline_init_run` | Initialize a new run; all phases pending; returns state + starting phases. |
| `pipeline_next_phases` | Resolve which phases can run next from state, artifacts, and DAG edges. |
| `pipeline_start_phase` | Mark a phase in-progress; returns its inputs/outputs/tools. |
| `pipeline_complete_phase` | Mark a phase as completed. |
| `pipeline_fail_phase` | Mark a phase as failed with an error message. |
| `pipeline_retry_phase` | Transition a failed phase back to pending so it can be re-started. |
| `pipeline_run_status` | Get current run state: phase statuses, artifacts, progress. |
| `pipeline_reload_state` | Reload active run state from disk after external edits. |
| `pipeline_phase_handoff` | Compact handoff payload for a fresh session at a phase boundary. |
| `pipeline_phase_brief` | Self-contained brief for executing a given phase. |

### Artifacts (6)

| Tool | Purpose |
|------|---------|
| `pipeline_validate_artifact` | Validate a JSON artifact against a pipeline schema (inline or file_path). |
| `pipeline_store_artifact` | Store a validated artifact and register it in run state. |
| `pipeline_register_artifact` | Register a large (>256KB) existing on-disk file as an artifact. |
| `pipeline_register_scaffold_outputs` | Scan a scaffold dir; register every file (idempotent). |
| `pipeline_load_artifact` | Load a stored artifact by key/filename; metadata only unless inline=true. |
| `pipeline_list_artifacts` | List all stored artifacts in a storage path. |

### Research / ingest (5)

| Tool | Purpose |
|------|---------|
| `pipeline_ingest_documents` | Ingest local files into a raw-collection artifact. |
| `pipeline_web_search` | Search the web and/or fetch+merge URLs into a collection. |
| `pipeline_analyze_codebase` | Analyze a package dir → codebase requirements artifact. |
| `pipeline_kb_search` | Search the knowledge base. |
| `pipeline_feature_request` | Record a feature suggestion into feature_requests.toml. |

### Concepts-collector (7)

| Tool | Purpose |
|------|---------|
| `pipeline_cc_load_collection` | Load a research collection from a JSON file into the store. |
| `pipeline_cc_query` | Query loaded research items by collection/tags/search/fields. |
| `pipeline_cc_get_items` | Fetch specific research items. |
| `pipeline_cc_collect_concepts` | Build a concept-extraction synthesis prompt from research items. |
| `pipeline_cc_save_overview` | Save a concept overview artifact. |
| `pipeline_cc_list_overviews` | List saved concept overviews. |
| `pipeline_cc_get_overview` | Retrieve a saved concept overview. |

### Synth (6)

| Tool | Purpose |
|------|---------|
| `pipeline_synth_load_collection` | Load a research collection for synthesis. |
| `pipeline_synth_query` | Query loaded items by collection/tags/search/fields. |
| `pipeline_synth_get_items` | Fetch specific items. |
| `pipeline_synth_create_spec` | Build a design-spec synthesis prompt from research items. |
| `pipeline_synth_save_spec` | Save a design-spec artifact. |
| `pipeline_synth_list_specs` | List saved design specs. |

### Debate (4)

| Tool | Purpose |
|------|---------|
| `pipeline_init_debate` | Initialize a structured multi-agent debate. |
| `pipeline_submit_argument` | Submit a round-1 argument. |
| `pipeline_synthesize_debate` | Synthesize the debate into a result. |
| `pipeline_save_debate` | Save the debate transcript. |

### Knowledge base (4)

| Tool | Purpose |
|------|---------|
| `pipeline_kb_search` | Semantic/keyword search over the knowledge base. |
| `pipeline_kb_sql_query` | Run a SQL query against the KB. |
| `pipeline_kb_build_index` | Build/refresh the KB index. |
| `pipeline_kb_export_query_log` | Export the KB query log. |

### Whole-run

| Tool | Purpose |
|------|---------|
| `pipeline_validate_run` | Cross-reference, semantic, and completeness validation of the run. |
