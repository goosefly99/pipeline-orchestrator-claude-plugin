import { readFileSync, existsSync } from 'node:fs'
import { parse } from 'smol-toml'
import type { PipelineConfig, PhaseDefinition, EdgeDefinition, DebateConfig, KBDefaults, StorageConfig, QualityGate, QualityCheck, HookConfig, HookTrigger } from './types.ts'

export function loadPipelineConfig(tomlPath: string): PipelineConfig {
  const raw = readFileSync(tomlPath, 'utf-8')
  const parsed = parse(raw) as Record<string, unknown>

  const pipeline = parsed.pipeline as { id: string; version: string; description: string }
  const rawPhases = parsed.phases as Record<string, Record<string, unknown>>
  const rawEdges = parsed.edges as Record<string, unknown>[]
  const rawDebate = parsed.debate as Record<string, unknown>
  const rawKB = parsed.knowledge_bases as Record<string, unknown> | undefined
  const rawSchemas = parsed.schemas as Record<string, string>
  const rawStorage = parsed.storage as Record<string, unknown>

  const phases: Record<string, PhaseDefinition> = {}
  for (const [name, raw] of Object.entries(rawPhases)) {
    phases[name] = {
      id: raw.id as number | string,
      description: raw.description as string,
      inputs: (raw.inputs as string[]) ?? [],
      outputs: (raw.outputs as string[]) ?? [],
      tools: (raw.tools as string[]) ?? [],
      input_mode: raw.input_mode as PhaseDefinition['input_mode'],
      output_mode: raw.output_mode as PhaseDefinition['output_mode'],
      entry_point: (raw.entry_point as boolean) ?? false,
      optional: raw.optional as boolean | undefined,
      reusable: raw.reusable as boolean | undefined,
      model_tier: raw.model_tier as PhaseDefinition['model_tier'],
      model: raw.model as string | undefined,
    }
  }

  const edges: EdgeDefinition[] = rawEdges.map(e => ({
    from: e.from as string,
    to: e.to as string,
    note: e.note as string | undefined,
    optional: e.optional as boolean | undefined,
    input_as: e.input_as as string | undefined,
    when_input: e.when_input as string | undefined,
  }))

  const debate: DebateConfig = {
    agents: {} as DebateConfig['agents'],
    rounds: (rawDebate.rounds as Record<string, string>) ?? {},
    output: (rawDebate.output as DebateConfig['output']) ?? {
      includes_transcript: true,
      includes_refined_artifact: true,
      artifact_version_bump: 'minor',
    },
  }
  const rawAgents = rawDebate.agents as Record<string, Record<string, unknown>> | undefined
  if (rawAgents) {
    for (const [name, agent] of Object.entries(rawAgents)) {
      debate.agents[name] = {
        role: agent.role as string,
        runs_in: agent.runs_in as string,
        depends_on: agent.depends_on as string[] | undefined,
      }
    }
  }

  const knowledge_bases: KBDefaults = {
    available_in: (rawKB?.available_in as string[]) ?? [],
    max_queries_per_phase: (rawKB?.max_queries_per_phase as number) ?? 20,
    query_mode: (rawKB?.query_mode as 'proactive' | 'on_demand') ?? 'proactive',
  }

  const storage: StorageConfig = {
    base_dir: (rawStorage.base_dir as string) ?? 'strategies',
    paths: (rawStorage.paths as Record<string, string>) ?? {},
  }

  return { pipeline, phases, edges, debate, knowledge_bases, schemas: rawSchemas, storage }
}

/**
 * Load quality gate configurations from a TOML file.
 * Returns an empty array if the file does not exist (gates are optional).
 *
 * Expected TOML structure:
 * ```toml
 * [[gates]]
 * phase = "curation"
 * on_failure = "block"
 *
 * [[gates.checks]]
 * check_type = "field_present"
 * description = "Sources must be present"
 * [gates.checks.params]
 * field = "sources"
 * ```
 */
export function loadQualityGates(tomlPath: string): QualityGate[] {
  if (!existsSync(tomlPath)) return []

  const raw = readFileSync(tomlPath, 'utf-8')
  const parsed = parse(raw) as Record<string, unknown>

  const rawGates = parsed.gates as Array<Record<string, unknown>> | undefined
  if (!rawGates || !Array.isArray(rawGates)) return []

  return rawGates.map(g => {
    const rawChecks = g.checks as Array<Record<string, unknown>> | undefined
    const checks: QualityCheck[] = (rawChecks ?? []).map(c => ({
      check_type: c.check_type as QualityCheck['check_type'],
      description: (c.description as string) ?? '',
      params: (c.params as Record<string, unknown>) ?? {},
    }))

    return {
      phase: g.phase as string,
      on_failure: (g.on_failure as 'warn' | 'block') ?? 'warn',
      checks,
    }
  })
}

/**
 * Load hook configurations from a TOML file's [hooks] section.
 * Returns an empty array if the file does not exist or has no hooks.
 *
 * Expected TOML structure (within pipeline.toml or separate hooks file):
 * ```toml
 * [[hooks]]
 * trigger = "pre_start"
 * phase_filter = "*"
 * command = "scripts/validate-env.sh"
 * args = ["--strict"]
 * timeout_ms = 5000
 * ```
 */
export function loadHooksConfig(tomlPath: string): HookConfig[] {
  if (!existsSync(tomlPath)) return []

  const raw = readFileSync(tomlPath, 'utf-8')
  const parsed = parse(raw) as Record<string, unknown>

  const rawHooks = parsed.hooks as Array<Record<string, unknown>> | undefined
  if (!rawHooks || !Array.isArray(rawHooks)) return []

  const validTriggers: HookTrigger[] = ['pre_pipeline_init', 'pre_start', 'post_complete', 'on_fail']

  return rawHooks
    .filter(h => {
      const trigger = h.trigger as string
      return validTriggers.includes(trigger as HookTrigger)
    })
    .map(h => ({
      trigger: h.trigger as HookTrigger,
      phase_filter: (h.phase_filter as string | undefined) ?? '*',
      command: h.command as string,
      args: (h.args as string[] | undefined) ?? [],
      timeout_ms: (h.timeout_ms as number | undefined) ?? 5000,
    }))
}
