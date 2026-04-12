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
