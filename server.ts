#!/usr/bin/env node

import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js'
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from '@modelcontextprotocol/sdk/types.js'
import { resolve, join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { existsSync } from 'node:fs'

import { loadPipelineConfig, loadQualityGates, loadHooksConfig } from './toml-loader.ts'
import { toolSchemas } from './tool-schemas.ts'
import { resolveNextPhases, getPhaseInputSatisfaction } from './dag.ts'
import { loadSchemas, validateArtifact } from './validator.ts'
import { storeArtifact, loadArtifact, listArtifacts, buildArtifactSummary } from './storage.ts'
import {
  initRun, startPhase, completePhase, failPhase, retryPhase, skipPhase,
  addArtifact, loadRunState, recoverRun, removePhaseArtifacts,
  computeRecommendedAction, computeRunWarnings,
} from './run-state.ts'
import { runGateChecks } from './quality-gates.ts'
import { runHooks, runPrePipelineInitHooks } from './hooks.ts'
import { PipelineError } from './types.ts'
import type { RunState, StorageConfig, QualityGate, HookConfig } from './types.ts'
import type { DebateState } from './debate.ts'
import { persistDebate as persistDebateToDisk } from './debate.ts'
import {
  handleInitDebate as initDebateHandler,
  handleSubmitArgument as submitArgumentHandler,
  handleSynthesizeDebate as synthesizeDebateHandler,
  handleSaveDebate as saveDebateHandler,
  type DebateContext,
  type SaveDebateContext,
} from './debate-handlers.ts'
import {
  handleCCLoadCollection as ccLoadCollectionHandler,
  handleCCQuery as ccQueryHandler,
  handleCCGetItems as ccGetItemsHandler,
  handleCCCollectConcepts as ccCollectConceptsHandler,
  handleCCSaveOverview as ccSaveOverviewHandler,
  handleCCListOverviews as ccListOverviewsHandler,
  handleCCGetOverview as ccGetOverviewHandler,
  createCCContext,
} from './cc-handlers.ts'
import { persistOverview as persistOverviewToDisk } from './concepts.ts'
import type { KnowledgeOverview } from './cc-types.ts'
import {
  handleSynthLoadCollection as synthLoadCollectionHandler,
  handleSynthQuery as synthQueryHandler,
  handleSynthGetItems as synthGetItemsHandler,
  handleSynthCreateSpec as synthCreateSpecHandler,
  handleSynthSaveSpec as synthSaveSpecHandler,
  handleSynthListSpecs as synthListSpecsHandler,
  createSynthContext,
} from './synth-handlers.ts'
import {
  handleValidateArtifact as validateArtifactHandler,
  handleStoreArtifact as storeArtifactHandler,
  handleRegisterArtifact as registerArtifactHandler,
  handleLoadArtifact as loadArtifactHandler,
  handleListArtifacts as listArtifactsHandler,
  type ArtifactContext,
} from './artifact-handlers.ts'
import {
  handleInitRun as initRunHandler,
  handleNextPhases as nextPhasesHandler,
  handleStartPhase as startPhaseHandler,
  handleCompletePhase as completePhaseHandler,
  handleFailPhase as failPhaseHandler,
  handleRetryPhase as retryPhaseHandler,
  handleRunStatus as runStatusHandler,
  handleReloadState as reloadStateHandler,
  handlePhaseHandoff as phaseHandoffHandler,
  handlePhaseBrief as phaseBriefHandler,
  type LifecycleContext,
} from './lifecycle-handlers.ts'
import {
  handleGetConfig as getConfigHandler,
  handleIngestDocuments as ingestDocumentsHandler,
  handleAnalyzeCodebase as analyzeCodebaseHandler,
  handleKBSearch as kbSearchHandler,
  handleKBSQLQuery as kbSQLQueryHandler,
  handleKBExportQueryLog as kbExportQueryLogHandler,
  handleKBBuildIndex as kbBuildIndexHandler,
  handleValidateRun as validateRunHandler,
  handleFeatureRequest as featureRequestHandler,
  handleWebSearch as webSearchHandler,
  handleRegisterScaffoldOutputs as registerScaffoldOutputsHandler,
  type GetConfigContext,
  type IngestContext,
  type ValidateRunContext,
  type KBBuildIndexContext,
  type WebSearchContext,
  type RegisterScaffoldOutputsContext,
} from './misc-handlers.ts'

const __dirname = dirname(fileURLToPath(import.meta.url))
const PIPELINE_DIR = resolve(__dirname, 'pipeline')
let _projectRoot: string | null = null

function setProjectRoot(root: string): void {
  _projectRoot = resolve(root)
}

function getProjectRoot(): string {
  if (_projectRoot) return _projectRoot
  throw new Error('project_root not set. Pass project_root in pipeline_init_run.')
}

let _config: ReturnType<typeof loadPipelineConfig> | null = null
let _schemas: ReturnType<typeof loadSchemas> | null = null

function getConfig() {
  if (!_config) {
    const tomlPath = join(PIPELINE_DIR, 'pipeline.toml')
    if (!existsSync(tomlPath))
      throw new Error(`pipeline.toml not found at ${tomlPath}\n  Bundled at ${PIPELINE_DIR}`)
    _config = loadPipelineConfig(tomlPath)
  }
  return _config
}

function getSchemas() {
  if (!_schemas) {
    const schemasDir = join(PIPELINE_DIR, 'schemas')
    if (!existsSync(schemasDir))
      throw new Error(`Pipeline schemas not found at ${schemasDir}\n  Bundled at ${PIPELINE_DIR}/schemas`)
    _schemas = loadSchemas(schemasDir)
  }
  return _schemas
}

let _qualityGates: QualityGate[] | null = null
let _hooksConfig: HookConfig[] | null = null

function getQualityGates(): QualityGate[] {
  if (!_qualityGates) {
    const gatesPath = join(PIPELINE_DIR, 'quality-gates.toml')
    _qualityGates = loadQualityGates(gatesPath)
  }
  return _qualityGates
}

function getHooksConfig(): HookConfig[] {
  if (!_hooksConfig) {
    const tomlPath = join(PIPELINE_DIR, 'pipeline.toml')
    _hooksConfig = loadHooksConfig(tomlPath)
  }
  return _hooksConfig
}

let activeRun: RunState | null = null
let activeRunDir = ''
let activeDebate: DebateState | null = null

function baseDirFromRunDir(runDir: string): string {
  const normalised = runDir.replace(/\\/g, '/')
  const idx = normalised.lastIndexOf('/runs/')
  if (idx !== -1) return runDir.slice(0, idx)
  return resolve(runDir, '../..')
}

function getStorageConfig(baseOverride?: string): StorageConfig {
  const base = baseOverride ?? resolve(getProjectRoot(), getConfig().storage.base_dir)
  return { base_dir: base, paths: getConfig().storage.paths }
}

function requireRun(): RunState {
  if (!activeRun) throw new Error('No active run. Call pipeline_init_run first.')
  return activeRun
}

const debateCtx: DebateContext = {
  getActiveDebate: () => activeDebate,
  setActiveDebate: (s) => { activeDebate = s },
}

const saveDebateCtx: SaveDebateContext = {
  ...debateCtx,
  validateArtifact: (...a) => validateArtifact(...a),
  getSchemas: () => getSchemas(),
  storeArtifact: (...a) => storeArtifact(...a),
  getStorageConfig: (bd) => getStorageConfig(bd),
  baseDirFromRunDir: (rd) => baseDirFromRunDir(rd),
  getProjectRoot: () => getProjectRoot(),
  getConfigStorageBaseDir: () => getConfig().storage.base_dir,
  getActiveRun: () => activeRun,
  getActiveRunDir: () => activeRunDir,
  addArtifact: (...a) => addArtifact(...a),
  setActiveRun: (s) => { activeRun = s },
  // Feature C: route debate transcript writes through run_data_dir when set,
  // with a legacy {base_dir}/debates fallback for runs that predate Feature-A.
  // Throws on collision unless force=true, matching storeArtifact semantics.
  persistDebate: (transcript: unknown, fileName: string, force?: boolean): string => {
    const legacyBaseDir = activeRun
      ? baseDirFromRunDir(activeRunDir)
      : resolve(getProjectRoot(), getConfig().storage.base_dir)
    return persistDebateToDisk(
      activeRun?.run_data_dir,
      legacyBaseDir,
      transcript,
      fileName,
      force,
    )
  },
}

const ccCtx = {
  ...createCCContext(),
  getBaseDir: (): string | null => {
    try {
      return resolve(getProjectRoot(), getConfig().storage.base_dir)
    } catch {
      return null
    }
  },
  // Feature C: route overview writes through run_data_dir when set, with a
  // legacy {base_dir}/overviews fallback for runs that predate Feature-A.
  persistOverview: (overview: KnowledgeOverview, outputDirOverride?: string): string => {
    const legacyBaseDir = activeRun
      ? baseDirFromRunDir(activeRunDir)
      : resolve(getProjectRoot(), getConfig().storage.base_dir)
    return persistOverviewToDisk(
      activeRun?.run_data_dir,
      legacyBaseDir,
      overview,
      outputDirOverride,
    )
  },
}

const synthCtx = {
  ...createSynthContext(''),
  getSpecsDir: (): string => {
    try {
      return resolve(getProjectRoot(), getConfig().storage.base_dir, getConfig().storage.paths.specs ?? 'specs')
    } catch {
      return resolve('./specs')
    }
  },
}

const artifactCtx: ArtifactContext = {
  getSchemas: () => getSchemas(),
  validateArtifact: (...a) => validateArtifact(...a),
  storeArtifact: (...a) => storeArtifact(...a),
  loadArtifact: (...a) => loadArtifact(...a),
  listArtifacts: (...a) => listArtifacts(...a),
  buildArtifactSummary: (...a) => buildArtifactSummary(...a),
  getStorageConfig: (bd) => getStorageConfig(bd),
  baseDirFromRunDir: (rd) => baseDirFromRunDir(rd),
  getProjectRoot: () => getProjectRoot(),
  getConfigStorageBaseDir: () => getConfig().storage.base_dir,
  getActiveRun: () => activeRun,
  setActiveRun: (s) => { activeRun = s },
  getActiveRunDir: () => activeRunDir,
  requireRun: () => requireRun(),
  addArtifact: (...a) => addArtifact(...a),
}

const lifecycleCtx: LifecycleContext = {
  getConfig: () => getConfig(),
  setProjectRoot: (root) => setProjectRoot(root),
  getStorageConfig: (bd) => getStorageConfig(bd),
  getActiveRun: () => activeRun,
  setActiveRun: (s) => { activeRun = s },
  getActiveRunDir: () => activeRunDir,
  setActiveRunDir: (dir) => { activeRunDir = dir },
  initRun: (...a) => initRun(...a),
  skipPhase: (...a) => skipPhase(...a),
  addArtifact: (...a) => addArtifact(...a),
  startPhase: (...a) => startPhase(...a),
  completePhase: (...a) => completePhase(...a),
  failPhase: (...a) => failPhase(...a),
  retryPhase: (...a) => retryPhase(...a),
  resolveNextPhases: (...a) => resolveNextPhases(...a),
  getPhaseInputSatisfaction: (...a) => getPhaseInputSatisfaction(...a),
  loadRunState: (...a) => loadRunState(...a),
  recoverRun: (...a) => recoverRun(...a),
  removePhaseArtifacts: (...a) => removePhaseArtifacts(...a),
  getQualityGates: () => getQualityGates(),
  runGateChecks: (...a) => runGateChecks(...a),
  getHooksConfig: () => getHooksConfig(),
  runHooks: (...a) => runHooks(...a),
  runPrePipelineInitHooks: (hooks, context, projectRoot) => runPrePipelineInitHooks(hooks, context, projectRoot),
  getProjectRoot: () => _projectRoot,
  computeRecommendedAction: (...a) => computeRecommendedAction(...a),
  computeRunWarnings: (...a) => computeRunWarnings(...a),
}

const getConfigCtx: GetConfigContext = {
  getProjectRoot: () => _projectRoot,
  getPipelineDir: () => PIPELINE_DIR,
  getConfig: () => getConfig(),
}

const ingestCtx: IngestContext = {
  validateArtifact: (...a) => validateArtifact(...a),
  getSchemas: () => getSchemas(),
  baseDirFromRunDir: (rd) => baseDirFromRunDir(rd),
  getProjectRoot: () => getProjectRoot(),
  getConfigStorageBaseDir: () => getConfig().storage.base_dir,
  getActiveRun: () => activeRun,
  getActiveRunDir: () => activeRunDir,
}

const validateRunCtx: ValidateRunContext = {
  requireRun: () => requireRun(),
  getStorageConfig: (bd) => getStorageConfig(bd),
  baseDirFromRunDir: (rd) => baseDirFromRunDir(rd),
  getActiveRunDir: () => activeRunDir,
}

const kbBuildIndexCtx: KBBuildIndexContext = {
  getProjectRoot: () => getProjectRoot(),
  getConfigStorageBaseDir: () => getConfig().storage.base_dir,
  getActiveRun: () => activeRun,
  getActiveRunDir: () => activeRunDir,
  baseDirFromRunDir: (rd) => baseDirFromRunDir(rd),
}

const webSearchCtx: WebSearchContext = {}

const scaffoldRegisterCtx: RegisterScaffoldOutputsContext = {
  requireRun: () => requireRun(),
  setActiveRun: (s) => { activeRun = s },
  getActiveRunDir: () => activeRunDir,
  baseDirFromRunDir: (rd) => baseDirFromRunDir(rd),
  addArtifact: (...a) => addArtifact(...a),
}

const server = new Server(
  { name: 'pipeline', version: '0.3.0' },
  {
    capabilities: { tools: {} },
    instructions: 'Pipeline orchestrator MCP server. Reads pipeline.toml to understand phases and DAG edges. ' +
      'Initialize a run, resolve available phases, validate artifacts, and manage artifact storage. ' +
      'Includes embedded concept extraction (pipeline_cc_*), research synthesis (pipeline_synth_*), and feature request tools. ' +
      'Workflow: pipeline_init_run → pipeline_next_phases → pipeline_start_phase → (do work) → ' +
      'pipeline_validate_artifact → pipeline_store_artifact → pipeline_complete_phase → repeat.',
  },
)

server.setRequestHandler(ListToolsRequestSchema, async () => ({ tools: toolSchemas }))

server.setRequestHandler(CallToolRequestSchema, async (req) => {
  const args = (req.params.arguments ?? {}) as Record<string, unknown>
  try {
    switch (req.params.name) {
      case 'pipeline_get_config': {
        const r = getConfigHandler(getConfigCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_init_run': {
        const r = initRunHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_next_phases': {
        const r = nextPhasesHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_start_phase': {
        const r = startPhaseHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_complete_phase': {
        const r = completePhaseHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_fail_phase': {
        const r = failPhaseHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_retry_phase': {
        const r = retryPhaseHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_run_status': {
        const r = runStatusHandler(lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_reload_state': {
        const r = reloadStateHandler(lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_phase_handoff': {
        const r = phaseHandoffHandler(lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_phase_brief': {
        const r = phaseBriefHandler(args, lifecycleCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_validate_artifact': {
        const r = validateArtifactHandler(args, artifactCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_store_artifact': {
        const r = storeArtifactHandler(args, artifactCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_register_artifact': {
        const r = registerArtifactHandler(args, artifactCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_register_scaffold_outputs': {
        const r = registerScaffoldOutputsHandler(args, scaffoldRegisterCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_load_artifact': {
        const r = loadArtifactHandler(args, artifactCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_list_artifacts': {
        const r = listArtifactsHandler(args, artifactCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_init_debate': {
        const r = initDebateHandler(args, debateCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_submit_argument': {
        const r = submitArgumentHandler(args, debateCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synthesize_debate': {
        const r = synthesizeDebateHandler(args, debateCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_save_debate': {
        const r = saveDebateHandler(args, saveDebateCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_load_collection': {
        const r = ccLoadCollectionHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_query': {
        const r = ccQueryHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_get_items': {
        const r = ccGetItemsHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_collect_concepts': {
        const r = await ccCollectConceptsHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_save_overview': {
        const r = ccSaveOverviewHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_list_overviews': {
        const r = ccListOverviewsHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_cc_get_overview': {
        const r = ccGetOverviewHandler(args, ccCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synth_load_collection': {
        const r = synthLoadCollectionHandler(args, synthCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synth_query': {
        const r = synthQueryHandler(args, synthCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synth_get_items': {
        const r = synthGetItemsHandler(args, synthCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synth_create_spec': {
        const r = synthCreateSpecHandler(args, synthCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synth_save_spec': {
        const r = synthSaveSpecHandler(args, synthCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_synth_list_specs': {
        const r = synthListSpecsHandler(args, synthCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_ingest_documents': {
        const r = await ingestDocumentsHandler(args, ingestCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_analyze_codebase': {
        const r = analyzeCodebaseHandler(args)
        return text(r.json, r.isError)
      }
      case 'pipeline_kb_search': {
        const r = kbSearchHandler(args, kbBuildIndexCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_kb_sql_query': {
        const r = await kbSQLQueryHandler(args)
        return text(r.json, r.isError)
      }
      case 'pipeline_kb_export_query_log': {
        const r = kbExportQueryLogHandler(args)
        return text(r.json, r.isError)
      }
      case 'pipeline_kb_build_index': {
        const r = kbBuildIndexHandler(args, kbBuildIndexCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_validate_run': {
        const r = validateRunHandler(args, validateRunCtx)
        return text(r.json, r.isError)
      }
      case 'pipeline_feature_request': {
        const r = featureRequestHandler(args)
        return text(r.json, r.isError)
      }
      case 'pipeline_web_search': {
        const r = await webSearchHandler(args, webSearchCtx)
        return text(r.json, r.isError)
      }

      default:
        return text(`Unknown tool: ${req.params.name}`, true)
    }
  } catch (err) {
    if (err instanceof PipelineError) {
      return text(JSON.stringify(err.toJSON()), true)
    }
    const msg = err instanceof Error ? err.message : String(err)
    return text(`Error: ${msg}`, true)
  }
})

function text(content: string, isError = false) {
  return { content: [{ type: 'text' as const, text: content }], isError }
}

const transport = new StdioServerTransport()
await server.connect(transport)

process.stderr.write('pipeline: MCP server started\n')

function shutdown() {
  server.close().catch(() => {})
  process.exit(0)
}

process.stdin.on('end', shutdown)
process.stdin.on('close', shutdown)
process.on('SIGTERM', shutdown)
process.on('SIGINT', shutdown)
process.on('unhandledRejection', (err: unknown) => {
  process.stderr.write(`pipeline: unhandled rejection: ${err}\n`)
})
process.on('uncaughtException', (err: Error) => {
  process.stderr.write(`pipeline: uncaught exception: ${err.message}\n`)
  shutdown()
})
