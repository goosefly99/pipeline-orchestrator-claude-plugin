// ── Pipeline Configuration (parsed from pipeline.toml) ─────

export interface PipelineConfig {
  pipeline: {
    id: string
    version: string
    description: string
  }
  phases: Record<string, PhaseDefinition>
  edges: EdgeDefinition[]
  debate: DebateConfig
  knowledge_bases: KBDefaults
  schemas: Record<string, string>
  storage: StorageConfig
}

export interface PhaseDefinition {
  id: number | string
  description: string
  inputs: string[]
  outputs: string[]
  tools: string[]
  input_mode?: 'any' | 'one_of' | 'one_of_primary' | 'required_optional'
  output_mode?: 'conditional'
  entry_point: boolean
  optional?: boolean
  reusable?: boolean
  /** Recommended model tier for this phase. */
  model_tier?: 'haiku' | 'sonnet' | 'opus'
  /**
   * Concrete Claude model ID override for this phase (e.g. `'claude-opus-4-6'`).
   * Feature B `pre_start` hooks use this at the top of the model precedence
   * chain: phase-specific `model` > `run_parameters.phase_model` > built-in
   * default. Set in `pipeline/pipeline.toml` under a `[[phases]]` entry.
   */
  model?: string
}

export interface EdgeDefinition {
  from: string
  to: string
  note?: string
  optional?: boolean
  input_as?: string
  when_input?: string
}

export interface DebateConfig {
  agents: Record<string, { role: string; runs_in: string; depends_on?: string[] }>
  rounds: Record<string, string>
  output: { includes_transcript: boolean; includes_refined_artifact: boolean; artifact_version_bump: string }
}

export interface KBDefaults {
  available_in: string[]
  max_queries_per_phase: number
  query_mode: 'proactive' | 'on_demand'
}

export interface StorageConfig {
  base_dir: string
  paths: Record<string, string>
}

// ── Run State ───────────────────────────────────────────────

export type PhaseStatus = 'pending' | 'in_progress' | 'completed' | 'skipped' | 'failed'

export interface PhaseState {
  phase_name: string
  status: PhaseStatus
  started_at?: string
  completed_at?: string
  input_artifacts: string[]
  output_artifacts: string[]
  error?: string
  retry_count: number
}

export interface RunState {
  run_id: string
  pipeline_version: string
  created_at: string
  updated_at: string
  completed_at?: string
  run_parameters?: PipelineRunParameters
  run_data_dir?: string
  status: 'initialized' | 'running' | 'completed' | 'failed'
  phases: Record<string, PhaseState>
  available_artifacts: ArtifactRef[]
  config_path: string
}

export interface ArtifactRef {
  type: string
  path: string
  phase: string
  created_at: string
  version?: number
  parent_artifact?: string
}

// ── Tool Annotations (MCP-standard concurrency hints) ──────

/**
 * MCP-standard tool annotations for client-side concurrency decisions.
 * Mirrors @modelcontextprotocol/sdk ToolAnnotations interface.
 */
export interface ToolAnnotations {
  /** A human-readable title for the tool. */
  title?: string
  /** If true, the tool does not modify its environment. Default: false */
  readOnlyHint?: boolean
  /** If true, the tool may perform destructive updates. Meaningful only when readOnlyHint is false. Default: true */
  destructiveHint?: boolean
  /** If true, calling the tool repeatedly with the same arguments has no additional effect. Default: false */
  idempotentHint?: boolean
  /** If true, this tool may interact with an open world of external entities. Default: true */
  openWorldHint?: boolean
}

// ── Response Envelope (workflow/mutation tools) ─────────────

export interface ResponseEnvelope {
  status: 'ok' | 'error'
  data: Record<string, unknown>
  next_step?: string
  warnings?: string[]
}

// ── Quality Gates ──────────────────────────────────────────

/** A single quality check within a gate. */
export interface QualityCheck {
  /** Unique check identifier (e.g., 'field_present', 'min_items', 'cross_ref_valid'). */
  check_type: 'field_present' | 'min_items' | 'cross_ref_valid'
  /** Human-readable description of what this check validates. */
  description: string
  /** Check-specific parameters (e.g., { field: 'sources', artifact_type: 'curated-collection' }). */
  params: Record<string, unknown>
}

/** Quality gate result for a single check. */
export type CheckResult = {
  check_type: string
  passed: boolean
  message: string
}

/** A quality gate applied to a specific phase. */
export interface QualityGate {
  /** Phase name this gate applies to. */
  phase: string
  /** Action on gate failure: 'warn' logs a warning, 'block' reverts the phase. */
  on_failure: 'warn' | 'block'
  /** Ordered list of checks to run for this gate. */
  checks: QualityCheck[]
}

/** Result of running all gate checks for a phase. */
export interface GateResult {
  phase: string
  passed: boolean
  on_failure: 'warn' | 'block'
  results: CheckResult[]
}

// ── Lifecycle Hooks ────────────────────────────────────────

/** Hook trigger points in the phase lifecycle. `pre_pipeline_init` is run-scoped (no phase); the others are phase-scoped. */
export type HookTrigger = 'pre_pipeline_init' | 'pre_start' | 'post_complete' | 'on_fail'

/** Configuration for a single lifecycle hook. */
export interface HookConfig {
  /** When this hook fires in the phase lifecycle. */
  trigger: HookTrigger
  /** Optional glob to filter which phases this hook applies to (e.g., '*', 'curation'). Defaults to '*'. */
  phase_filter?: string
  /** Command to execute — absolute path or relative to project root. */
  command: string
  /** Arguments to pass to the command. */
  args?: string[]
  /** Timeout in milliseconds for the hook command. Defaults to 5000. */
  timeout_ms?: number
}

/**
 * Run-scoped parameters collected by `pre_pipeline_init` hooks before the
 * `pipeline_init_run` handler mutates state. Feature B reads `phase_model`
 * to pick the model for per-phase subagents; Feature C reads `run_name` and
 * `run_directory_timestamp` to compute the per-run data directory.
 */
export interface PipelineRunParameters {
  /** Short human-readable identifier, e.g. "fix-quality-gate-mismatch". Used by Feature C for the run data directory name. */
  run_name?: string
  /** Claude model ID for per-phase subagents spawned by Feature B, e.g. "claude-sonnet-4-6". */
  phase_model?: string
  /** Identifier of the spec this run operates on. */
  target_spec_id?: string
  /** Identifier of the feature this run operates on. */
  target_feature?: string
  /** ISO-like timestamp, filesystem-safe. Format: `YYYY-MM-DDTHH-MM-SSZ` (colons replaced with hyphens). */
  run_directory_timestamp?: string
  /** Extensible for future parameters. */
  [key: string]: unknown
}

/**
 * Directive returned by a `pre_start` hook telling the MCP client (Claude
 * Code) how to spawn the subagent that will execute the phase. When present
 * in the `pipeline_start_phase` response, the client MUST spawn an Agent
 * subagent with these exact parameters; that subagent then owns
 * `pipeline_complete_phase` / `pipeline_fail_phase` for the phase. The
 * orchestrator does not wait synchronously — subsequent `pipeline_next_phases`
 * calls in the parent session observe the state written by the subagent.
 *
 * See Feature B in `pipeline_mcp_data/scaffold/IMPLEMENTATION-PLAN.md`.
 */
export interface AgentDirective {
  /** Claude Code subagent type, e.g. `'general-purpose'`. */
  subagent_type: string
  /**
   * Resolved Claude model ID. Resolution precedence:
   * phase-specific `PhaseDefinition.model` > `run_parameters.phase_model` >
   * hard-coded default (`'claude-sonnet-4-6'`).
   */
  model: string
  /** Short, human-readable description of the task — surfaced in the Agent tool call. */
  description: string
  /** Full subagent prompt — typically the phase brief + completion instructions + a no-context-inheritance rule. */
  prompt: string
  /** Optional git-worktree isolation request for agents that mutate the repo. */
  isolation?: 'worktree'
}

/** Lifecycle event record for events.jsonl audit trail. */
export interface LifecycleEvent {
  timestamp: string
  event:
    | 'phase_started'
    | 'phase_completed'
    | 'phase_failed'
    | 'hook_executed'
    | 'hook_failed'
    | 'gate_checked'
    | 'gate_evaluated'
    | 'artifact_stored'
  phase: string
  run_id: string
  details?: Record<string, unknown>
}

// ── Handler Response ─────────────────────────────────────────

/** Shared return type for all handler modules. */
export interface HandlerResponse {
  json: string
  isError?: boolean
}

// ── Structured Error Classification ─────────────────────────

/** 9 error classes covering all known pipeline failure modes. */
export type ErrorClass =
  | 'validation_error'
  | 'state_transition_error'
  | 'file_io_error'
  | 'schema_mismatch'
  | 'artifact_not_found'
  | 'phase_not_ready'
  | 'configuration_error'
  | 'gate_blocked'
  | 'unknown_error'

/**
 * Structured pipeline error — thrown by handler modules for known failure modes.
 * server.ts catches instanceof PipelineError and serializes to a structured JSON
 * error response. Unknown errors fall through to existing text(msg, true) behavior.
 */
export class PipelineError extends Error {
  readonly error_class: ErrorClass
  readonly recovery_action: string
  readonly retryable: boolean
  readonly details: Record<string, unknown>

  constructor(
    message: string,
    error_class: ErrorClass,
    options?: {
      recovery_action?: string
      retryable?: boolean
      details?: Record<string, unknown>
      cause?: unknown
    },
  ) {
    super(message, options?.cause != null ? { cause: options.cause } : undefined)
    this.name = 'PipelineError'
    this.error_class = error_class
    this.recovery_action = options?.recovery_action ?? ''
    this.retryable = options?.retryable ?? false
    this.details = options?.details ?? {}
  }

  /** Serialize to structured JSON for MCP error responses. */
  toJSON(): Record<string, unknown> {
    return {
      error_class: this.error_class,
      message: this.message,
      recovery_action: this.recovery_action,
      retryable: this.retryable,
      details: this.details,
    }
  }
}
