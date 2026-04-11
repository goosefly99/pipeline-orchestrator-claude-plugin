# pipeline-orchestrator-claude-plugin

Pipeline orchestrator MCP server — reads `pipeline.toml`, resolves a phase DAG, validates artifacts against JSON schemas, manages run state, and coordinates research, debate, concept-collection, and synthesis phases through a single MCP surface.

## What it does

`pipeline-orchestrator` is a Model Context Protocol server that turns a declarative `pipeline.toml` definition into an executable multi-phase workflow. It owns run state, enforces phase ordering via a DAG, validates every artifact produced by a phase against a JSON schema, and exposes lifecycle operations (init, start, complete, fail, retry) as MCP tools. It also bundles concept-collection, synthesis, debate, codebase analysis, and knowledge-base tools so an agent can drive the entire pipeline from a single server.

Key tool groups (~40 tools total, all prefixed `pipeline_`):

- **Lifecycle** — `pipeline_init_run`, `pipeline_next_phases`, `pipeline_start_phase`, `pipeline_complete_phase`, `pipeline_fail_phase`, `pipeline_retry_phase`, `pipeline_run_status`, `pipeline_reload_state`, `pipeline_phase_handoff`, `pipeline_phase_brief`
- **Artifacts** — `pipeline_validate_artifact`, `pipeline_store_artifact`, `pipeline_register_artifact`, `pipeline_register_scaffold_outputs`, `pipeline_load_artifact`, `pipeline_list_artifacts`, `pipeline_validate_run`
- **Research / ingestion** — `pipeline_ingest_documents`, `pipeline_web_search`, `pipeline_analyze_codebase`, `pipeline_kb_search`, `pipeline_kb_sql_query`, `pipeline_kb_build_index`, `pipeline_kb_export_query_log`
- **Concepts collector** — `pipeline_cc_load_collection`, `pipeline_cc_query`, `pipeline_cc_get_items`, `pipeline_cc_collect_concepts`, `pipeline_cc_save_overview`, `pipeline_cc_list_overviews`, `pipeline_cc_get_overview`
- **Synth** — `pipeline_synth_load_collection`, `pipeline_synth_query`, `pipeline_synth_get_items`, `pipeline_synth_create_spec`, `pipeline_synth_save_spec`, `pipeline_synth_list_specs`
- **Debate** — `pipeline_init_debate`, `pipeline_submit_argument`, `pipeline_synthesize_debate`, `pipeline_save_debate`
- **Misc** — `pipeline_get_config`, `pipeline_feature_request`

## Installation

Inside Claude Code:

```
/plugin install goosefly99/pipeline-orchestrator-claude-plugin
```

Or clone manually:

```
git clone https://github.com/goosefly99/pipeline-orchestrator-claude-plugin.git
cd pipeline-orchestrator-claude-plugin
npm install
```

## Configuration

The server registers itself via `.mcp.json` and is launched through `start.mjs`, which lazily runs `npm install` on first start. No environment variables are required.

Configuration files:

- `pipeline/pipeline.toml` — phase DAG, inputs, outputs, gate bindings
- `pipeline/quality-gates.toml` — gate definitions referenced by phases
- `pipeline/schemas/*.json` — JSON schemas for every pipeline artifact
- `hooks/hooks-config.toml` — optional pre/post phase hooks

Runtime data (all gitignored):

- `pipeline_mcp_data/` — run state, artifacts, collections, specs, overviews
- `hooks/hook-logs/` — hook execution logs
- `hooks/runtime-config.json` — generated hook runtime config
