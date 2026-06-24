---
name: pipeline-handoff
description: Pass context across a pipeline-orchestrator phase or session boundary. Use when handing context from a completing phase to the next, or resuming a run in a fresh session — produce a compact handoff with pipeline_phase_handoff, then consume it with pipeline_phase_brief.
user-invocable: true
---

# pipeline-handoff — carry context across boundaries

Use this skill to move context across a **phase boundary** or a **session
boundary** in a pipeline-orchestrator run, instead of dragging the full prior
conversation forward. It pairs two tools — one that *produces* a compact context
payload, one that *consumes* it — so a fresh agent can pick up exactly where the
last one left off without quadratic context growth.

## When to use

- A phase just completed and you want to brief whoever (or whatever session)
  runs the next phase.
- You are resuming a run in a **new session** and need to rehydrate just enough
  state to continue, without replaying everything that came before.

## The produce → consume pairing

**Produce — `pipeline_phase_handoff`.** Call this at a phase boundary to emit a
compact handoff payload (kept under ~5K tokens). It contains the run identity,
each phase's status with artifact paths (references only — not the artifact
bodies), the DAG edges, the phases that can run next, quality-gate summaries,
and a suggested instruction for the next step. Because it carries *refs, not
content*, it is cheap to hand to a fresh session and lets you reset context at
phase boundaries to avoid quadratic context growth.

**Consume — `pipeline_phase_brief`** *(phase)*. When the next session or
subagent enters a specific phase, call this to get a self-contained brief for
that phase: its description, input artifact paths, output requirements,
quality-gate criteria, storage paths, the tools to use, and a structured
instruction. It is designed to be passed **directly as a subagent prompt** — no
additional context needed.

The two compose naturally: `pipeline_phase_handoff` gives the *whole-run*
orientation at the boundary (where we are, what's next, where the artifacts
live); `pipeline_phase_brief` gives the *single-phase* execution detail once you
commit to running a particular next phase.

## Cross-session continuity

When you resume a run in a brand-new session:

1. If state may have changed on disk since the run was last touched, re-sync
   first with `pipeline_reload_state` (see the `pipeline-run-status` skill).
2. Read the handoff payload from `pipeline_phase_handoff` to re-orient: run
   identity, phase statuses, artifact refs, and the next runnable phases.
3. Pick the phase to run and call `pipeline_phase_brief` for it.
4. Hand that brief to the executing agent and proceed — typically by returning
   to the main loop in the `pipeline-orchestrate` skill (`pipeline_start_phase`
   onward).

This keeps each session's working context small: only the compact handoff and
the single phase brief cross the boundary, never the accumulated history.

## Relationship to the other skills

- To actually advance phases (start / validate / store / complete), use
  `pipeline-orchestrate`.
- For read-only status checks (current phase, what's next, artifact inspection),
  use `pipeline-run-status`.
