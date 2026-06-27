# AGENTS.md — pipeline-orchestrator

Guidance for any coding agent (Codex, Claude, or other MCP client) that drives the
`pipeline-orchestrator` MCP server. This server is a pure-Python FastMCP process,
launched by its `.mcp.json` as `uv run --directory <plugin-root> python -m
pipeline_orchestrator` over stdio. Its server identity is `pipeline`; every tool it
exposes is prefixed `pipeline_`.

## What it does

The orchestrator turns a declarative `pipeline.toml` definition into an executable
multi-phase workflow. It reads that file to learn the **phases** and the typed **DAG
edges** between them, then owns the run state for a single pipeline run. The default
DAG (`research-to-implementation`) flows: document ingestion and research discovery →
curation → concept extraction → design synthesis → optional adversarial debate →
validation → implementation scaffold, with an independent codebase-analysis entry that
feeds synthesis, debate, validation, and scaffolding. Every artifact a phase produces
is validated against a JSON schema before it is stored, and phase ordering is enforced
by the DAG rather than left to the agent's judgement.

You generally do not call the lifecycle tools by hand reasoning about the DAG yourself —
you ask the server what is runnable (`pipeline_next_phases`), do the work the phase
brief describes, then validate and store the result. The server is the source of truth
for "what can run next" and "is this artifact well-formed".

## Core lifecycle workflow

The canonical sequence for advancing a run is:

1. `pipeline_get_config` — read the phases, DAG edges, storage paths, and schema refs.
2. `pipeline_init_run` — create run state (all phases pending) for a `run_id` and
   project root. Returns the available starting phases.
3. `pipeline_next_phases` — resolve which phases are runnable given completed phases and
   available artifacts.
4. `pipeline_phase_brief` (or `pipeline_start_phase`) — get a self-contained brief for a
   phase: its inputs, output requirements, quality-gate criteria, and storage paths.
   `pipeline_start_phase` marks it in-progress and returns its definition.
5. Do the phase work, producing the required artifact(s).
6. `pipeline_validate_artifact` — check the artifact against its pipeline schema.
7. `pipeline_store_artifact` — persist the validated artifact and register it in run
   state. Refuses to overwrite an existing file unless `force` is set.
8. `pipeline_complete_phase` — mark the phase done (or `pipeline_fail_phase` /
   `pipeline_retry_phase` on error).
9. Repeat from step 3 until no phases remain, then `pipeline_validate_run`.

At a phase or session boundary, use `pipeline_phase_handoff` to emit a compact
(<5K-token) payload and `pipeline_phase_brief` to resume in a fresh session, instead of
dragging the full prior conversation forward.

## Tool families (43 tools, all prefixed `pipeline_`)

**Lifecycle** — manage run state and phase transitions:
`pipeline_get_config`, `pipeline_init_run`, `pipeline_next_phases`,
`pipeline_start_phase`, `pipeline_complete_phase`, `pipeline_fail_phase`,
`pipeline_retry_phase`, `pipeline_run_status`, `pipeline_reload_state`,
`pipeline_phase_handoff`, `pipeline_phase_brief`.

**Artifacts** — validate, store, and load typed artifacts:
`pipeline_validate_artifact`, `pipeline_store_artifact`, `pipeline_register_artifact`
(register a large file already on disk, >256KB), `pipeline_register_scaffold_outputs`
(register every file in a scaffold dir), `pipeline_load_artifact` (metadata-only by
default; opt into content with `inline`), `pipeline_list_artifacts`,
`pipeline_validate_run` (cross-reference, semantic, and completeness checks).

**Research and ingestion**:
`pipeline_ingest_documents` (PDF, Markdown, text, CSV, HTML, JSON, YAML, TOML,
notebooks → a raw-collection), `pipeline_web_search` (search snippets and/or fetch-merge
URLs into a collection), `pipeline_analyze_codebase` (produce a codebase-requirements
artifact).

**Knowledge base** — over an indexed collection for a run/phase:
`pipeline_kb_search` (BM25 ranked text), `pipeline_kb_sql_query` (named SQL template),
`pipeline_kb_build_index` (build the BM25 index), `pipeline_kb_export_query_log`.

**Concepts collector** (`pipeline_cc_*`) — extract structured concepts from research:
`pipeline_cc_load_collection`, `pipeline_cc_query`, `pipeline_cc_get_items`,
`pipeline_cc_collect_concepts`, `pipeline_cc_save_overview`,
`pipeline_cc_list_overviews`, `pipeline_cc_get_overview`.

**Synthesis** (`pipeline_synth_*`) — combine research items into design specs:
`pipeline_synth_load_collection`, `pipeline_synth_query`, `pipeline_synth_get_items`,
`pipeline_synth_create_spec`, `pipeline_synth_save_spec`, `pipeline_synth_list_specs`.

**Debate** — adversarial multi-agent review of an artifact:
`pipeline_init_debate` (returns agent prompts for parallel dispatch),
`pipeline_submit_argument`, `pipeline_synthesize_debate`, `pipeline_save_debate`.

**Misc** — `pipeline_feature_request` (append an improvement idea to a
`feature_requests.toml`).

## Key invariants

- **Validate before store.** Never persist an artifact that has not passed
  `pipeline_validate_artifact` against its schema; the DAG's gates assume every stored
  artifact is schema-valid. `pipeline_store_artifact` will not silently overwrite —
  pass `force` only when overwrite is intentional.
- **Respect the state machine.** `start` requires a pending phase; `complete` and `fail`
  require an in-progress phase; only failed phases can be retried. Ask
  `pipeline_next_phases` rather than forcing an out-of-order transition.
- **Mutual exclusion with the Node sibling.** A frozen TypeScript implementation lives
  under `legacy-node/`. It registers the same `pipeline` server identity and the same
  `pipeline_*` tool names. Only one of the two may be enabled at a time — the pure-Python
  server here is canonical; never enable both, or tool registration collides.
- **The vector index is read-only here.** The COLD vector store (the OS second-brain at
  the configured index path) is a pointer; this plugin reads it but never writes there.
  Local BM25 indices under the run's `kb` storage are the only index it builds.
- **Single-client, stdio transport.** stdout is the JSON-RPC channel; the server logs
  only to stderr. Run state and artifacts live under the run's `pipeline_mcp_data`
  storage tree.
