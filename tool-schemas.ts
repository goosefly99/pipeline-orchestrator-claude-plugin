// tool-schemas.ts — MCP tool definitions extracted from server.ts
// Each entry is { name, description, inputSchema, annotations?, _meta? }
// passed to ListToolsRequestSchema handler.

import type { ToolAnnotations } from './types.ts'

export interface ToolSchema {
  name: string
  description: string
  inputSchema: {
    type: 'object'
    properties: Record<string, unknown>
    required?: string[]
    additionalProperties?: unknown
  }
  /** MCP-standard annotations for client-side concurrency decisions (readOnlyHint, destructiveHint, etc.) */
  annotations?: ToolAnnotations
  /** MCP _meta field for internal hints (e.g., max_result_chars for result budgeting) */
  _meta?: {
    /** Maximum characters for tool result payload. 50000 for queries, 10000 for mutations. */
    max_result_chars?: number
    [key: string]: unknown
  }
}

export const toolSchemas: ToolSchema[] = [
  {
    name: 'pipeline_get_config',
    description: 'Get the pipeline configuration: phases, edges, debate config, storage paths, and schema references.',
    inputSchema: { type: 'object', properties: {} },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_init_run',
    description: 'Initialize a new pipeline run. Creates run state with all phases set to pending. Returns the run state and available starting phases.',
    inputSchema: {
      type: 'object',
      properties: {
        run_id: { type: 'string', description: 'Unique run identifier' },
        project_root: { type: 'string', description: 'Absolute path to the project directory. Artifacts are stored under <project_root>/pipeline_mcp_data/.' },
        base_dir: { type: 'string', description: 'Override base directory for artifact storage (overrides project_root-based default)' },
        skip_phases: {
          type: 'array', items: { type: 'string' },
          description: 'Phases to skip in this run',
        },
        initial_artifacts: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              type: { type: 'string' },
              path: { type: 'string' },
            },
            required: ['type', 'path'],
          },
          description: 'Pre-existing artifacts to register (e.g., existing collections)',
        },
        run_parameters: {
          type: 'object',
          description: 'Optional run-scoped parameters (e.g. run_name, phase_model, target_spec_id). Merged with pre_pipeline_init hook output when a hook is registered; hook defaults lose to explicit values here. Persisted to state.run_parameters.',
          additionalProperties: true,
        },
      },
      required: ['run_id', 'project_root'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_next_phases',
    description: 'Resolve which phases can run next based on current run state: completed phases, available artifacts, and DAG edges.',
    inputSchema: {
      type: 'object',
      properties: {
        skip_phases: {
          type: 'array', items: { type: 'string' },
          description: 'Additional phases to skip beyond those set at init',
        },
      },
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_start_phase',
    description: 'Mark a phase as in-progress. Returns the phase definition (inputs, outputs, tools) so the executor knows what to do.',
    inputSchema: {
      type: 'object',
      properties: {
        phase: { type: 'string', description: 'Phase name to start' },
      },
      required: ['phase'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_complete_phase',
    description: 'Mark a phase as completed.',
    inputSchema: {
      type: 'object',
      properties: {
        phase: { type: 'string', description: 'Phase name to complete' },
      },
      required: ['phase'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_fail_phase',
    description: 'Mark a phase as failed with an error message.',
    inputSchema: {
      type: 'object',
      properties: {
        phase: { type: 'string', description: 'Phase name that failed' },
        error: { type: 'string', description: 'Error description' },
      },
      required: ['phase', 'error'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_retry_phase',
    description: 'Retry a failed phase by transitioning it back to pending so it can be started again. Only phases in the failed state can be retried.',
    inputSchema: {
      type: 'object',
      properties: {
        phase: { type: 'string', description: 'Phase name to retry' },
      },
      required: ['phase'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_validate_artifact',
    description: 'Validate a JSON artifact against one of the pipeline schemas. Returns validation result with any errors. Provide either "artifact" (inline JSON) or "file_path" (path to an existing JSON file on disk).',
    inputSchema: {
      type: 'object',
      properties: {
        schema: {
          type: 'string',
          description: 'Schema filename (e.g., "raw-collection.json", "design-spec.json")',
        },
        artifact: {
          type: 'object',
          description: 'The artifact JSON to validate (omit if using file_path)',
        },
        file_path: {
          type: 'string',
          description: 'Absolute or base_dir-relative path to an existing artifact JSON file on disk. Use instead of artifact for large files that exceed MCP payload limits.',
        },
      },
      required: ['schema'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_store_artifact',
    description: 'Store a validated artifact to the correct directory and register it in run state. Provide either "artifact" (inline JSON) or "file_path" (path to an existing JSON file on disk). When file_path is provided the file is copied into the storage directory; when artifact is provided it is written directly.',
    inputSchema: {
      type: 'object',
      properties: {
        storage_key: {
          type: 'string',
          description: 'Storage path key (e.g., "raw_collections", "specs", "overviews", "debates"). Canonical storage keys by artifact type: `design-spec` → `"specs"`, `validation-report` → `"reports"`, `knowledge-overview` → `"overviews"`, `debate-transcript` → `"debates"`, `curated-collection` → `"curated-collections"`, `raw-collection` → `"raw-collections"`, `research-manifest` → `"manifests"`, `codebase-requirements` → `"codebase"`, `scaffold-document` → `"scaffold"`. Storing an artifact type under the wrong key can mask other artifacts.',
        },
        file_name: { type: 'string', description: 'Filename for the artifact' },
        artifact: { type: 'object', description: 'The artifact JSON to store (omit if using file_path)' },
        file_path: { type: 'string', description: 'Absolute or base_dir-relative path to an existing artifact JSON file on disk. Use instead of artifact for large files that exceed MCP payload limits.' },
        artifact_type: { type: 'string', description: 'Schema type name (e.g., "raw-collection", "design-spec")' },
        phase: { type: 'string', description: 'Phase that produced this artifact' },
        force: { type: 'boolean', description: 'If true, overwrite an existing artifact at the same path. Default false.' },
        parent_artifact: { type: 'string', description: 'File path of the parent artifact this was derived from (e.g. the pre-debate version). Used to record lineage for debate-refined artifacts.' },
      },
      required: ['storage_key', 'file_name', 'artifact_type', 'phase'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_register_artifact',
    description: 'Register an already-existing file on disk as a pipeline artifact without passing its content inline. Use this for large artifacts (>256KB) that already exist on disk. The file is verified to exist, optionally validated against a schema, and registered in the run state.',
    inputSchema: {
      type: 'object',
      properties: {
        file_path: {
          type: 'string',
          description: 'Absolute path or path relative to base_dir of the existing artifact file',
        },
        artifact_type: {
          type: 'string',
          description: 'Schema type name (e.g., "raw-collection", "design-spec")',
        },
        phase: {
          type: 'string',
          description: 'Phase that produced this artifact',
        },
        storage_key: {
          type: 'string',
          description: 'Storage path key — used to verify the file lives under the expected directory',
        },
        validate: {
          type: 'boolean',
          description: 'If true, load and validate the file against the schema named "<artifact_type>.json". Defaults to false.',
        },
      },
      required: ['file_path', 'artifact_type', 'phase', 'storage_key'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_register_scaffold_outputs',
    description: 'Scan a scaffold directory and register every file as a pipeline artifact. Files named "scaffold-manifest.json" become "scaffold-manifest" artifacts; all other files become "scaffold-document" artifacts. Idempotent: files already registered in run-state are skipped. Call this once at the end of the implementation_scaffold phase after writing all scaffold documents.',
    inputSchema: {
      type: 'object',
      properties: {
        scaffold_dir: {
          type: 'string',
          description: 'Path to the scaffold directory (absolute or relative to the storage base dir). Defaults to "scaffold" under the storage base dir.',
        },
        phase: {
          type: 'string',
          description: 'Phase name under which to register the artifacts. Defaults to "implementation_scaffold".',
        },
      },
      required: [],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_load_artifact',
    description: 'Load a previously stored artifact by storage key and filename. By default returns metadata and path only (no content). Pass inline=true (or full=true) to retrieve full artifact content — caller explicitly opts into the context cost. Pass fields to retrieve only specific top-level keys.',
    inputSchema: {
      type: 'object',
      properties: {
        storage_key: { type: 'string' },
        file_name: { type: 'string' },
        inline: { type: 'boolean', description: 'If true, return full artifact content inline. Default false (returns metadata/path only). Alias for full.' },
        full: { type: 'boolean', description: 'If true, return the full artifact content. Default false (returns summary). Alias for inline.' },
        fields: {
          type: 'array',
          items: { type: 'string' },
          description: 'Optional list of top-level keys to return from the artifact. Ignored when inline=true or full=true.',
        },
      },
      required: ['storage_key', 'file_name'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_list_artifacts',
    description: 'List all stored artifacts in a given storage path.',
    inputSchema: {
      type: 'object',
      properties: {
        storage_key: { type: 'string' },
      },
      required: ['storage_key'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_run_status',
    description: 'Get the current run state: phase statuses, available artifacts, and overall progress.',
    inputSchema: { type: 'object', properties: {} },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_ingest_documents',
    description: [
      'Ingest local files (PDF, Markdown, text, CSV, HTML, JSON, YAML, TOML, Jupyter notebooks) into a raw-collection artifact.',
      'JSON files containing arrays are automatically split into individual items.',
      'Returns the raw-collection JSON. Use pipeline_store_artifact afterwards to persist it.',
      'Supports PDF via pdf-parse. All other types use built-in Node.js APIs.',
    ].join(' '),
    inputSchema: {
      type: 'object',
      properties: {
        file_paths: {
          type: 'array',
          items: { type: 'string' },
          description: 'Absolute paths to the files to ingest.',
        },
        ingest_name: {
          type: 'string',
          description: 'Name prefix for the collection_id (e.g. "strategy-docs", "market-research"). Defaults to "ingest".',
        },
        validate: {
          type: 'boolean',
          description: 'If true, validate the produced collection against raw-collection.json schema before returning. Defaults to true.',
        },
        json_items_key: {
          type: 'string',
          description: 'For JSON files: key containing the array of items to split (e.g., "posts", "items"). Auto-detected if omitted.',
        },
      },
      required: ['file_paths'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },

  {
    name: 'pipeline_web_search',
    description: [
      'Search the web for additional resources and optionally fetch and merge content into an existing raw-collection.',
      'Two modes: (1) provide queries to get search result snippets for review;',
      '(2) provide fetch_urls to fetch pages and merge extracted text into the collection.',
      'Both may be combined in a single call — searches first, then fetches.',
    ].join(' '),
    inputSchema: {
      type: 'object',
      properties: {
        queries: {
          type: 'array',
          items: { type: 'string' },
          description: 'Search queries to execute (triggers search mode)',
        },
        collection_path: {
          type: 'string',
          description: 'Absolute path to the existing raw-collection JSON file to merge into',
        },
        max_results_per_query: {
          type: 'number',
          description: 'Maximum search results per query (default 5)',
        },
        fetch_urls: {
          type: 'array',
          items: { type: 'string' },
          description: 'URLs to fetch and merge into the collection (triggers fetch mode)',
        },
      },
      required: ['collection_path'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },

  // ── Debate tools ──────────────────────────────────────────────

  {
    name: 'pipeline_init_debate',
    description: 'Start an adversarial debate on an artifact (knowledge-overview or design-spec). Initialises debate state and returns agent prompts for parallel dispatch.',
    inputSchema: {
      type: 'object',
      properties: {
        input_type: {
          type: 'string',
          enum: ['knowledge-overview', 'design-spec'],
          description: 'Type of artifact being debated',
        },
        input_id: { type: 'string', description: 'ID of the artifact being debated' },
        input_version: { type: 'string', description: 'Version of the artifact being debated' },
        artifact_content: { type: 'object', description: 'The full artifact JSON to debate' },
        artifact_path: { type: 'string', description: 'Filesystem path of the stored artifact. Returned in the response so callers can load it once and combine with each role prompt when dispatching agents.' },
        codebase_requirements_id: { type: 'string', description: 'Optional codebase requirements artifact ID' },
        domain_profile: {
          description: 'Domain profile for debate prompts. Pass a string ("finance", "software", "research") for a built-in profile, or an object with {name, risk_vocabulary[], domain_constraints[], specialist_focus}.',
        },
      },
      required: ['input_type', 'input_id', 'input_version', 'artifact_content'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_submit_argument',
    description: 'Record one round-1 agent argument in the active debate.',
    inputSchema: {
      type: 'object',
      properties: {
        role: {
          type: 'string',
          enum: ['advocate', 'critic', 'risk_specialist', 'domain_specialist'],
          description: 'Agent role submitting this argument',
        },
        position: { type: 'string', description: 'Agent position / overall stance' },
        evidence: { type: 'array', items: { type: 'string' }, description: 'Supporting evidence points' },
        counterpoints: { type: 'array', items: { type: 'string' }, description: 'Counterpoints raised' },
        proposed_changes: { type: 'array', items: { type: 'string' }, description: 'Specific changes proposed' },
        confidence: { type: 'number', description: 'Confidence score 0–1' },
      },
      required: ['role', 'position', 'confidence'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_synthesize_debate',
    description: 'Two-step: first call (no changes_accepted) returns the synthesizer agent prompt. Second call (with changes_accepted) records the synthesis result.',
    inputSchema: {
      type: 'object',
      properties: {
        changes_accepted: {
          type: 'array',
          items: { type: 'string' },
          description: 'Changes accepted by the synthesizer',
        },
        changes_rejected: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              proposed: { type: 'string' },
              reason: { type: 'string' },
            },
          },
          description: 'Changes rejected with reasons',
        },
        open_questions: {
          type: 'array',
          items: { type: 'string' },
          description: 'Open questions remaining after synthesis',
        },
        output_version: { type: 'string', description: 'Version of the output artifact' },
        kb_queries_used: { type: 'boolean', description: 'Whether KB queries were used during synthesis' },
      },
      required: ['changes_accepted', 'changes_rejected'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_save_debate',
    description: 'Validate and save the completed debate transcript to storage. Clears active debate state.',
    inputSchema: {
      type: 'object',
      properties: {
        file_name: { type: 'string', description: 'Filename for the saved transcript' },
        phase: { type: 'string', description: 'Pipeline phase that produced this debate' },
      },
      required: ['file_name', 'phase'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },

  // ── Analysis tools ────────────────────────────────────────────

  {
    name: 'pipeline_analyze_codebase',
    description: 'Analyze a package directory to produce a codebase requirements artifact.',
    inputSchema: {
      type: 'object',
      properties: {
        codebase_path: { type: 'string', description: 'Absolute path to the package directory to analyze' },
      },
      required: ['codebase_path'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },

  // ── Knowledge Base tools ──────────────────────────────────────

  {
    name: 'pipeline_kb_search',
    description: 'Run a BM25 ranked text search over an indexed collection for a given run/phase.',
    inputSchema: {
      type: 'object',
      properties: {
        run_id: { type: 'string', description: 'Pipeline run identifier' },
        phase: { type: 'string', description: 'Pipeline phase name' },
        query: { type: 'string', description: 'Natural language query string' },
        top_k: { type: 'number', description: 'Number of results to return (default 5)' },
        filter: { type: 'object', description: 'Optional metadata filter (e.g. { tags: ["crypto"] })' },
      },
      required: ['run_id', 'phase', 'query'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_kb_sql_query',
    description: 'Run a named SQL template query against the knowledge base for a given run/phase.',
    inputSchema: {
      type: 'object',
      properties: {
        kb_config: { type: 'object', description: 'Knowledge base connection configuration' },
        run_id: { type: 'string', description: 'Pipeline run identifier' },
        phase: { type: 'string', description: 'Pipeline phase name' },
        template_name: { type: 'string', description: 'Name of the SQL template to execute' },
        parameters: { type: 'object', description: 'Optional parameters for the SQL template' },
      },
      required: ['kb_config', 'run_id', 'phase', 'template_name'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_kb_build_index',
    description: 'Build a BM25 text search index from a loaded in-memory collection.',
    inputSchema: {
      type: 'object',
      required: ['collection_name'],
      properties: {
        collection_name: {
          type: 'string',
          description: 'Name of the in-memory collection to index (must be loaded via pipeline_cc_load_collection first)',
        },
      },
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_kb_export_query_log',
    description: 'Export the KB query log for a given run/phase, optionally scoped to a debate transcript.',
    inputSchema: {
      type: 'object',
      properties: {
        run_id: { type: 'string', description: 'Pipeline run identifier' },
        phase: { type: 'string', description: 'Pipeline phase name' },
        debate_transcript_id: { type: 'string', description: 'Optional debate transcript ID to filter by' },
      },
      required: ['run_id', 'phase'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },

  // ── Validation and state tools ────────────────────────────────

  {
    name: 'pipeline_validate_run',
    description: 'Run cross-reference, semantic, and completeness validation on the active pipeline run. Checks that artifact ID references resolve, enforces consistency rules, and verifies completed phases produced expected outputs. Returns a structured report.',
    inputSchema: {
      type: 'object',
      properties: {
        phase_output_overrides: {
          type: 'object',
          description: 'Optional overrides for expected phase outputs. Keys are phase names, values are arrays of expected artifact type strings.',
          additionalProperties: {
            type: 'array',
            items: { type: 'string' },
          },
        },
      },
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_reload_state',
    description: 'Reload the active run state from disk. Use this after external modifications to run-state.json.',
    inputSchema: { type: 'object', properties: {} },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_feature_request',
    description: [
      'Record a feature suggestion into a feature_requests.toml file at a specified directory.',
      'Appends to an existing file or creates a new one.',
      'Use this to capture improvement ideas while working in the codebase or pipeline.',
      'Reviewing agents can later read these files when designing update specs.',
    ].join(' '),
    inputSchema: {
      type: 'object',
      properties: {
        target_directory: {
          type: 'string',
          description: 'Absolute path to the directory where feature_requests.toml should be written. Must be an existing directory.',
        },
        title: {
          type: 'string',
          description: 'Short name for the proposed feature (max 200 chars).',
        },
        description: {
          type: 'string',
          description: 'What the feature does and why it is useful.',
        },
        priority: {
          type: 'string',
          enum: ['low', 'medium', 'high'],
          description: 'Priority level: low, medium, or high.',
        },
        scope: {
          type: 'string',
          description: 'What part of the codebase this feature affects (e.g. file path, module, directory).',
        },
        rationale: {
          type: 'string',
          description: 'Why this feature is worth adding.',
        },
        source: {
          type: 'string',
          description: 'Identifier for the requesting agent or process. Defaults to "agent".',
        },
      },
      required: ['target_directory', 'title', 'description', 'priority', 'scope', 'rationale'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },

  // ── Concepts Collector tools ──────────────────────────────────

  {
    name: 'pipeline_cc_load_collection',
    description:
      'Load a JSON research collection with auto-detected schema mapping. ' +
      'Accepts any JSON file with an array of items — field mapping is inferred automatically. ' +
      'Returns collection summary with item count, detected fields, and available tags.',
    inputSchema: {
      type: 'object',
      properties: {
        file_path: {
          type: 'string',
          description: 'Path to the JSON file to load',
        },
        name: {
          type: 'string',
          description: 'Name for this collection (defaults to filename)',
        },
        field_overrides: {
          type: 'object',
          description:
            'Override auto-detected field mappings. Keys: items_key, id_field, content_field, title_field, tags_field, author_field, date_field, url_field',
        },
      },
      required: ['file_path'],
    },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_cc_query',
    description:
      'Search and filter items across loaded collections. ' +
      'Filter by tags, full-text search, or metadata field values. ' +
      'Returns matching items with truncated content previews.',
    inputSchema: {
      type: 'object',
      properties: {
        collection: {
          type: 'string',
          description: 'Collection name to search (omit to search all)',
        },
        tags: {
          type: 'array',
          items: { type: 'string' },
          description: 'Filter items that have any of these tags',
        },
        search: {
          type: 'string',
          description: 'Full-text search term',
        },
        fields: {
          type: 'object',
          description: 'Filter by metadata fields (e.g., {"strategy_type": "arbitrage"})',
        },
        limit: {
          type: 'number',
          description: 'Max results to return (default 20)',
        },
      },
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_cc_get_items',
    description:
      'Get specific items by ID from a collection. ' +
      'Content is truncated to max_content_length characters by default (500). ' +
      'Pass full=true to retrieve complete item content without truncation. ' +
      'Use after pipeline_cc_query to retrieve item data.',
    inputSchema: {
      type: 'object',
      properties: {
        collection: {
          type: 'string',
          description: 'Collection name',
        },
        item_ids: {
          type: 'array',
          items: { type: 'string' },
          description: 'Array of item IDs to retrieve',
        },
        max_content_length: {
          type: 'integer',
          description: 'Maximum number of characters to return per item content field. Defaults to 500. Ignored when full=true.',
        },
        full: {
          type: 'boolean',
          description: 'If true, return the complete item content without truncation. Default false.',
        },
      },
      required: ['collection', 'item_ids'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_cc_collect_concepts',
    description:
      'Extract and organize key concepts from selected research items into a structured ' +
      'knowledge base overview. Accepts items from multiple collections. Returns a synthesis ' +
      'prompt with all source material, cross-reference analysis, and a knowledge overview ' +
      'template for you to complete. ' +
      'For large collections (more than 15 items), chunked processing is used automatically: ' +
      'one chunk of 8 items is returned at a time with a continuation_token. ' +
      'Call pipeline_cc_collect_concepts again with the continuation_token to get the next chunk, then merge concept results across all chunks.',
    inputSchema: {
      type: 'object',
      properties: {
        title: {
          type: 'string',
          description: 'Title for the knowledge overview',
        },
        items: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              collection: { type: 'string' },
              item_ids: {
                type: 'array',
                items: { type: 'string' },
              },
            },
            required: ['collection', 'item_ids'],
          },
          description: 'Items to analyze, grouped by collection',
        },
        focus: {
          type: 'string',
          description: 'Optional focus area to prioritize in the analysis',
        },
        depth: {
          type: 'string',
          enum: ['brief', 'standard', 'deep'],
          description: 'Analysis depth: brief (3-5 concepts), standard (all concepts), deep (exhaustive with relationships)',
        },
        chunk_index: {
          type: 'integer',
          description: '0-based chunk index for large collections (more than 15 items). Ignored when total items are 15 or fewer.',
        },
        continuation_token: {
          type: 'string',
          description: 'Token returned by a previous chunk response to advance to the next chunk. Takes priority over chunk_index when both are provided.',
        },
      },
      required: ['title', 'items'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_cc_save_overview',
    description:
      'Save a completed knowledge overview to disk. ' +
      'Pass the filled-in overview JSON from pipeline_cc_collect_concepts.',
    inputSchema: {
      type: 'object',
      properties: {
        overview: {
          type: 'object',
          description: 'The completed KnowledgeOverview JSON object',
        },
        output_dir: {
          type: 'string',
          description: 'Directory to save to (defaults to configured output dir)',
        },
      },
      required: ['overview'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_cc_list_overviews',
    description:
      'List all saved knowledge overviews with their metadata.',
    inputSchema: {
      type: 'object',
      properties: {
        directory: {
          type: 'string',
          description: 'Directory to list from (defaults to configured output dir)',
        },
      },
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_cc_get_overview',
    description:
      'Retrieve a previously saved knowledge overview by its ID.',
    inputSchema: {
      type: 'object',
      properties: {
        overview_id: {
          type: 'string',
          description: 'The overview ID to retrieve',
        },
        directory: {
          type: 'string',
          description: 'Directory to search (defaults to configured output dir)',
        },
      },
      required: ['overview_id'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },

  // ── Synth tools ──────────────────────────────────────────────

  {
    name: 'pipeline_synth_load_collection',
    description:
      'Load a JSON research collection into memory for synthesis. Auto-detects the array of items, ' +
      'ID field, content field, tags, author, and date fields. Returns a summary of the collection.',
    inputSchema: {
      type: 'object',
      properties: {
        file_path: { type: 'string', description: 'Absolute path to the JSON collection file' },
        name: { type: 'string', description: 'Friendly name for the collection (defaults to filename)' },
        items_key: { type: 'string', description: 'Override: key for the items array (auto-detected)' },
        id_field: { type: 'string', description: 'Override: field name for item IDs (auto-detected)' },
        content_field: { type: 'string', description: 'Override: field name for item content (auto-detected)' },
        title_field: { type: 'string', description: 'Override: field name for item title/summary (auto-detected)' },
        tags_field: { type: 'string', description: 'Override: field name for item tags (auto-detected)' },
      },
      required: ['file_path'],
    },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_synth_query',
    description:
      'Search and filter items across loaded research collections for synthesis. ' +
      'Supports tag filtering, full-text search, and arbitrary metadata field matching.',
    inputSchema: {
      type: 'object',
      properties: {
        collection: { type: 'string', description: 'Collection name to search (searches all if omitted)' },
        tags: { type: 'array', items: { type: 'string' }, description: 'Filter by tags' },
        search: { type: 'string', description: 'Full-text search across title, content, and tags' },
        fields: { type: 'object', description: 'Filter by metadata field values' },
        limit: { type: 'number', description: 'Max results (default 20)' },
      },
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_synth_get_items',
    description: 'Get the full content and metadata of specific items by ID from a loaded collection.',
    inputSchema: {
      type: 'object',
      properties: {
        collection: { type: 'string', description: 'Collection name' },
        item_ids: { type: 'array', items: { type: 'string' }, description: 'Array of item IDs to retrieve' },
      },
      required: ['collection', 'item_ids'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_synth_create_spec',
    description:
      'Create a structured design spec by synthesising multiple research items. Returns the full ' +
      'source material, cross-reference analysis, and a spec template to complete.',
    inputSchema: {
      type: 'object',
      properties: {
        title: { type: 'string', description: 'Title for the design spec' },
        items: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              collection: { type: 'string', description: 'Collection name' },
              item_id: { type: 'string', description: 'Item ID' },
              relevance: { type: 'string', description: 'Why this item is relevant' },
            },
            required: ['collection', 'item_id'],
          },
          description: 'Items to synthesise — can span multiple collections',
        },
        spec_type: {
          type: 'string',
          enum: ['implementation', 'architecture', 'research', 'comparison'],
          description: 'Type of spec to generate (default: implementation)',
        },
        domain: { type: 'string', description: 'Domain context (e.g. "trading", "web development")' },
        focus: { type: 'string', description: 'Additional constraints or focus areas for the synthesis' },
      },
      required: ['title', 'items'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_synth_save_spec',
    description: 'Save a completed design spec to disk as JSON. Creates the output directory if needed.',
    inputSchema: {
      type: 'object',
      properties: {
        spec: { type: 'object', description: 'The completed DesignSpec JSON object' },
        output_dir: { type: 'string', description: 'Directory to save in (defaults to pipeline specs storage path)' },
      },
      required: ['spec'],
    },
    annotations: { destructiveHint: true },
    _meta: { max_result_chars: 10000 },
  },
  {
    name: 'pipeline_synth_list_specs',
    description: 'List all saved design specs with their title, status, type, and source count.',
    inputSchema: {
      type: 'object',
      properties: {
        directory: { type: 'string', description: 'Specs directory (defaults to pipeline specs storage path)' },
      },
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_phase_handoff',
    description: 'Generate a compact handoff payload (<5K tokens) for starting a fresh agent session at a phase boundary. Contains run identity, phase statuses with artifact paths (refs only), DAG edges, next phases, quality gate summaries, and suggested instruction. Use this at phase boundaries to enable session resets that reduce quadratic context growth.',
    inputSchema: {
      type: 'object',
      properties: {},
    },
    annotations: { readOnlyHint: true, idempotentHint: true },
    _meta: { max_result_chars: 50000 },
  },
  {
    name: 'pipeline_phase_brief',
    description: 'Generate a self-contained subagent brief for executing a given phase. Contains phase description, input artifact paths, output requirements, quality gate criteria, storage paths, tools, and structured instruction. Designed to be passed directly as a subagent prompt — no additional context needed.',
    inputSchema: {
      type: 'object',
      properties: {
        phase: { type: 'string', description: 'Name of the phase to generate a brief for.' },
      },
      required: ['phase'],
    },
    annotations: { readOnlyHint: true },
    _meta: { max_result_chars: 50000 },
  },
]
