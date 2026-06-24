---
name: pipeline-run-status
description: Read-only situational awareness for a pipeline-orchestrator run. Use when checking what phase a run is in, what is complete, what is runnable next, inspecting produced artifacts, re-syncing state after a restart or external edit, or validating whole-run integrity.
user-invocable: true
---

# pipeline-run-status — situational awareness

Use this skill to **understand the current state of a pipeline-orchestrator run
without changing it**. Every tool here is read-only: nothing starts, completes,
fails, or retries a phase. When you actually need to advance the run, switch to
the `pipeline-orchestrate` skill.

## When to use

- "What phase is the run in?" / "What's done?" / "What can run next?"
- You just restarted, or someone edited `run-state.json` outside the tools, and
  you need to re-sync before doing anything.
- You want to inspect an artifact a previous phase produced.
- You want a whole-run integrity check before declaring the run finished.

## Tools

1. **`pipeline_run_status`** — the primary snapshot: current run state, every
   phase's status, the available artifacts, and overall progress. Start here.

2. **`pipeline_next_phases`** — the authoritative set of phases that are
   **runnable right now**, derived from completed phases, available artifacts,
   and the DAG edges. This is what you would act on if you were orchestrating;
   here it just tells you where the run can go next. An empty result means the
   run is terminal.

3. **`pipeline_list_artifacts`** — list all stored artifacts in a given storage
   path, to see what each completed phase actually produced.

4. **`pipeline_load_artifact`** — load a specific stored artifact by storage key
   and filename. By default it returns **metadata and path only**; pass
   `inline=true` (or `full=true`) to pull the full content (you explicitly opt
   into the context cost), or `fields` to retrieve only specific top-level keys.

5. **`pipeline_reload_state`** — reload the active run state from disk. Use this
   after a restart or after external modifications to `run-state.json`, so your
   subsequent reads reflect what is actually on disk rather than a stale view.

6. **`pipeline_validate_run`** — run cross-reference, semantic, and completeness
   validation across the whole run: checks that artifact ID references resolve,
   enforces consistency rules, and verifies completed phases produced their
   expected outputs. Returns a structured report. This inspects integrity only —
   it does not modify state.

## Typical sequence

For a quick check: `pipeline_run_status` → `pipeline_next_phases`.

After a restart or external edit: `pipeline_reload_state` first, then
`pipeline_run_status`.

To dig into outputs: `pipeline_list_artifacts` → `pipeline_load_artifact`
(metadata first; only inline the content you truly need).

Before declaring a run complete: `pipeline_validate_run` and read the report.

Remember: none of these transition a phase. To start, complete, fail, or retry
a phase, use `pipeline-orchestrate`. To move context across a phase or session
boundary, use `pipeline-handoff`.
