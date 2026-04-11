#!/usr/bin/env node
// hooks/pre-pipeline-init.example.js
//
// Reference implementation of a `pre_pipeline_init` lifecycle hook for the
// pipeline-orchestrator MCP server. This hook runs BEFORE `pipeline_init_run`
// mutates any state and its job is to populate `PipelineRunParameters` (used
// by downstream features B and C) and/or emit user prompts when required
// input is missing.
//
// How the hook runner invokes this script:
//   1. The runner passes a JSON-encoded `PreInitHookContext` via the
//      PIPELINE_HOOK_CONTEXT environment variable.
//      Shape: { trigger: 'pre_pipeline_init', project_root, requested_args }
//   2. This script writes a single JSON object to stdout describing the
//      parameters to merge into the new RunState and any user prompts.
//      Shape: { parameters: { ... }, userPrompts: [ ... ] }
//   3. Exit code 0 on success. Any non-zero exit causes the runner to abort
//      `pipeline_init_run` with a configuration_error.
//
// Users are expected to copy this file and customize the defaults to match
// their own environment (model selection, run naming policy, prompts, etc.).

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
  const context = readContext()
  const requestedArgs =
    context && typeof context.requested_args === 'object' && context.requested_args !== null
      ? context.requested_args
      : {}

  // Default phase model for Feature B subagents. Customize as needed.
  const phaseModel = 'claude-sonnet-4-6'

  // Windows-safe filesystem timestamp: replace both ':' and '.' with '-'.
  const runDirectoryTimestamp = new Date().toISOString().replace(/[:.]/g, '-')

  const parameters = {
    phase_model: phaseModel,
    run_directory_timestamp: runDirectoryTimestamp,
  }

  const userPrompts = []

  // Derive `run_name` from `requested_args.run_id` when no explicit name was
  // supplied. Otherwise, ask the user for a name via the userPrompts channel.
  if (typeof requestedArgs.run_name === 'string' && requestedArgs.run_name.length > 0) {
    parameters.run_name = requestedArgs.run_name
  } else if (typeof requestedArgs.run_id === 'string' && requestedArgs.run_id.length > 0) {
    parameters.run_name = requestedArgs.run_id
  } else {
    userPrompts.push('Please provide a short run_name for this pipeline run.')
  }

  process.stdout.write(JSON.stringify({ parameters, userPrompts }, null, 2))
  process.exit(0)
}

main()
