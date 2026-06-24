"""Core data models (spec §3.3, R2 §4) — ported from ``legacy-node/types.ts``.

Filled in M1 (T1.2) alongside ``run_state.py``. Each dataclass mirrors the
corresponding TypeScript interface field-for-field (names + optionality). The
TS ``?:`` optional properties become ``Optional`` fields defaulting to ``None``;
required mutable collections default via ``field(default_factory=...)`` so
construction matches the Node object literals in ``run-state.ts``.

These are plain mutable dataclasses (not frozen): the run-state machine mutates
``PhaseState`` / ``RunState`` in place exactly as the Node code mutates its
object literals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── Pipeline configuration literals (mirror the TS unions in types.ts) ───

InputMode = Literal["any", "one_of", "one_of_primary", "required_optional"]
"""``types.ts`` ``PhaseDefinition.input_mode`` union (``?:`` optional → ``None``)."""

OutputMode = Literal["conditional"]
"""``types.ts`` ``PhaseDefinition.output_mode`` union (``?:`` optional → ``None``)."""

ModelTier = Literal["haiku", "sonnet", "opus"]
"""``types.ts`` ``PhaseDefinition.model_tier`` union (``?:`` optional → ``None``)."""

QueryMode = Literal["proactive", "on_demand"]
"""``types.ts`` ``KBDefaults.query_mode`` union."""

CheckType = Literal["field_present", "min_items", "cross_ref_valid"]
"""``types.ts`` ``QualityCheck.check_type`` union."""

OnFailure = Literal["warn", "block"]
"""``types.ts`` ``QualityGate.on_failure`` union."""

HookTrigger = Literal["pre_pipeline_init", "pre_start", "post_complete", "on_fail"]
"""``types.ts`` ``HookTrigger`` union — the four valid lifecycle-hook triggers."""


# ── Status literals (mirror the TS string unions in types.ts) ────────

PhaseStatus = Literal["pending", "in_progress", "completed", "skipped", "failed"]
"""``types.ts``: ``'pending' | 'in_progress' | 'completed' | 'skipped' | 'failed'``."""

RunStatus = Literal["initialized", "running", "completed", "failed"]
"""``types.ts`` ``RunState.status`` union (success terminal state is ``completed``)."""

LifecycleEventName = Literal[
    "phase_started",
    "phase_completed",
    "phase_failed",
    "hook_executed",
    "hook_failed",
    "gate_checked",
    "gate_evaluated",
    "artifact_stored",
]
"""``types.ts`` ``LifecycleEvent.event`` union."""


@dataclass
class ArtifactRef:
    """Reference to a stored artifact (mirrors TS ``ArtifactRef``).

    ``version`` / ``parent_artifact`` are the ``?:`` optional fields; they
    default to ``None`` and are normalized on load (``version`` → ``1``).
    """

    type: str
    path: str
    phase: str
    created_at: str
    version: int | None = None
    parent_artifact: str | None = None


@dataclass
class PhaseState:
    """Per-phase state within a run (mirrors TS ``PhaseState``).

    ``started_at`` / ``completed_at`` / ``error`` are the ``?:`` optionals.
    ``input_artifacts`` / ``output_artifacts`` are required arrays that default
    to empty lists (matching the ``initRun`` object literal). ``retry_count``
    defaults to ``0``.
    """

    phase_name: str
    status: PhaseStatus
    started_at: str | None = None
    completed_at: str | None = None
    input_artifacts: list[str] = field(default_factory=list)
    output_artifacts: list[str] = field(default_factory=list)
    error: str | None = None
    retry_count: int = 0


@dataclass
class RunState:
    """Top-level run state (mirrors TS ``RunState``).

    Required fields come first; the ``?:`` optionals (``completed_at``,
    ``run_parameters``, ``run_data_dir``) default to ``None``. ``phases`` and
    ``available_artifacts`` are required containers defaulting to empty.
    """

    run_id: str
    pipeline_version: str
    created_at: str
    updated_at: str
    status: RunStatus
    config_path: str
    phases: dict[str, PhaseState] = field(default_factory=dict)
    available_artifacts: list[ArtifactRef] = field(default_factory=list)
    completed_at: str | None = None
    run_parameters: dict[str, object] | None = None
    run_data_dir: str | None = None


@dataclass
class LifecycleEvent:
    """A run/phase lifecycle event for the ``events.jsonl`` audit trail.

    Mirrors TS ``LifecycleEvent``. ``resolved_model`` / ``details`` are the
    ``?:`` optionals. (No ``run_state.py`` function emits these yet — the
    ``events.jsonl`` writer lives in the lifecycle-handler layer, a later task;
    this dataclass exists so downstream modules can name the type.)
    """

    timestamp: str
    event: LifecycleEventName
    phase: str
    run_id: str
    resolved_model: str | None = None
    details: dict[str, object] | None = None


# ── Pipeline configuration (parsed from pipeline.toml) ───────────────
#
# Mirrors the ``types.ts`` config interfaces field-for-field. These live here
# (not in ``dag.py``) so ``toml_loader.py`` (T1.5) and the dag/run-state layers
# share one definition. The dag functions in ``dag.py`` operate on
# :class:`PipelineConfig` / :class:`PhaseDefinition` / :class:`EdgeDefinition`.


@dataclass
class PhaseDefinition:
    """A single pipeline phase (mirrors TS ``PhaseDefinition``).

    ``id`` is ``number | string`` in TS (``int | str`` here). ``inputs`` /
    ``outputs`` / ``tools`` are required arrays defaulting to empty lists (the
    Node test's ``makeConfig`` fills them via ``?? []``). The ``?:`` optionals
    (``input_mode`` / ``output_mode`` / ``optional`` / ``reusable`` /
    ``model_tier`` / ``model``) default to ``None``/``False``; ``entry_point``
    is required and defaults to ``False`` (matching ``makeConfig``'s
    ``?? false``).
    """

    id: int | str
    description: str = ""
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    entry_point: bool = False
    input_mode: InputMode | None = None
    output_mode: OutputMode | None = None
    optional: bool | None = None
    reusable: bool | None = None
    model_tier: ModelTier | None = None
    model: str | None = None


@dataclass
class EdgeDefinition:
    """A directed DAG edge (mirrors TS ``EdgeDefinition``).

    The TOML wire key is ``from`` (a Python reserved word), so the field is named
    ``from_``; the loader/caller (e.g. T1.5 ``toml_loader``) maps the wire key
    ``from`` onto ``from_`` when constructing edges — there is no field-metadata
    mechanism (the field is a bare ``from_: str``). ``to`` is required; the rest
    (``note`` / ``optional`` / ``input_as`` / ``when_input``) are the ``?:``
    optionals.
    """

    from_: str
    to: str
    note: str | None = None
    optional: bool | None = None
    input_as: str | None = None
    when_input: str | None = None


@dataclass
class PipelineMeta:
    """The ``[pipeline]`` table (mirrors TS ``PipelineConfig.pipeline``)."""

    id: str
    version: str
    description: str


@dataclass
class DebateOutputConfig:
    """The ``debate.output`` sub-object (mirrors TS ``DebateConfig.output``)."""

    includes_transcript: bool
    includes_refined_artifact: bool
    artifact_version_bump: str


@dataclass
class DebateAgentConfig:
    """A single debate agent (mirrors the TS ``DebateConfig.agents`` value)."""

    role: str
    runs_in: str
    depends_on: list[str] | None = None


@dataclass
class DebateConfig:
    """The ``[debate]`` table (mirrors TS ``DebateConfig``)."""

    agents: dict[str, DebateAgentConfig] = field(default_factory=dict)
    rounds: dict[str, str] = field(default_factory=dict)
    output: DebateOutputConfig | None = None


@dataclass
class KBDefaults:
    """Knowledge-base defaults (mirrors TS ``KBDefaults``)."""

    available_in: list[str] = field(default_factory=list)
    max_queries_per_phase: int = 20
    query_mode: QueryMode = "proactive"


@dataclass
class StorageConfig:
    """Artifact storage layout (mirrors TS ``StorageConfig``)."""

    base_dir: str = ""
    paths: dict[str, str] = field(default_factory=dict)


@dataclass
class PipelineConfig:
    """Top-level parsed ``pipeline.toml`` (mirrors TS ``PipelineConfig``).

    ``phases`` is a ``Record<string, PhaseDefinition>`` (keyed by phase name);
    ``edges`` is an ordered list (edge filter order is observable in
    ``resolve_input_artifacts``). The dag functions read only ``phases`` and
    ``edges``; the remaining fields exist for full-config parity with T1.5.
    """

    pipeline: PipelineMeta
    phases: dict[str, PhaseDefinition] = field(default_factory=dict)
    edges: list[EdgeDefinition] = field(default_factory=list)
    debate: DebateConfig | None = None
    knowledge_bases: KBDefaults | None = None
    schemas: dict[str, str] = field(default_factory=dict)
    storage: StorageConfig | None = None


# ── Quality gates (parsed from quality-gates.toml) ───────────────────
#
# Mirrors the ``types.ts`` ``QualityCheck`` / ``QualityGate`` interfaces.
# ``toml_loader.load_quality_gates`` builds these; the gate-evaluation layer
# (a later milestone) consumes them. ``CheckResult`` / ``GateResult`` (the
# *run-time* evaluation outputs in ``types.ts``) are not yet needed and land
# with that layer.


@dataclass
class QualityCheck:
    """A single quality check within a gate (mirrors TS ``QualityCheck``).

    ``description`` defaults to ``""`` (the Node loader's ``?? ''``); ``params``
    is a free-form mapping defaulting to ``{}`` (the loader's ``?? {}``).
    """

    check_type: CheckType
    description: str = ""
    params: dict[str, object] = field(default_factory=dict)


@dataclass
class QualityGate:
    """A quality gate applied to a specific phase (mirrors TS ``QualityGate``).

    ``on_failure`` defaults to ``"warn"`` (the loader's ``?? 'warn'``); ``checks``
    is an ordered list defaulting to empty.
    """

    phase: str
    on_failure: OnFailure = "warn"
    checks: list[QualityCheck] = field(default_factory=list)


# ── Lifecycle hooks (parsed from a [[hooks]] array) ──────────────────
#
# Mirrors the ``types.ts`` ``HookConfig`` interface. The TS ``?:`` optionals
# (``phase_filter`` / ``args`` / ``timeout_ms``) are filled with their nullish
# defaults by ``toml_loader.load_hooks_config`` (``'*'`` / ``[]`` / ``5000``),
# so they are plain non-optional fields here carrying those same defaults.


@dataclass
class HookConfig:
    """Configuration for a single lifecycle hook (mirrors TS ``HookConfig``).

    ``phase_filter`` defaults to ``"*"``, ``args`` to ``[]``, ``timeout_ms`` to
    ``5000`` — the same nullish defaults the loader applies.
    """

    trigger: HookTrigger
    command: str
    phase_filter: str = "*"
    args: list[str] = field(default_factory=list)
    timeout_ms: int = 5000
