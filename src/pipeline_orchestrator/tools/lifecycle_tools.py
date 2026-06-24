"""Lifecycle MCP tool bodies (port of ``legacy-node/lifecycle-handlers.ts``).

The lifecycle handlers (``handle_init_run`` / ``handle_next_phases`` /
``handle_start_phase`` / ``handle_complete_phase`` / ``handle_fail_phase`` /
``handle_retry_phase`` / ``handle_run_status`` / ``handle_reload_state`` /
``handle_phase_handoff`` / ``handle_phase_brief``) take a :class:`LifecycleContext`
DI object — the seam ``server.py`` wires to the real run-state / storage / dag /
gate / hook functions — and return a
:class:`~pipeline_orchestrator.models.HandlerResponse`. Plus the two events.jsonl
writers (:func:`append_event` / :func:`append_phase_completed_event`) and the
two stale-state helpers (:func:`detect_stale_state` / :func:`reload_state_from_disk`).

Wire format (§4.2 / §5):

* The structured envelopes (``handle_complete_phase`` / ``handle_fail_phase`` /
  ``handle_retry_phase`` and the ``user_input_required`` short-circuit of
  ``handle_init_run``) serialize a ``{status, data, next_step?, warnings?}``
  object via ``json.dumps(obj, indent=2, ensure_ascii=False)`` with field order
  matching the TS object literal (status, data, next_step, warnings).
* The plain-object responses (``handle_init_run`` success / ``handle_next_phases``
  / ``handle_start_phase`` / ``handle_run_status`` / ``handle_reload_state`` /
  ``handle_phase_handoff`` / ``handle_phase_brief``) serialize the raw response
  dict via ``json.dumps(obj, indent=2, ensure_ascii=False)``.
* The events.jsonl writer serializes one event per line via
  ``json.dumps(event, separators=(",", ":"), ensure_ascii=False)`` (single-line,
  matching the Node ``JSON.stringify(event)``).
* **``ensure_ascii=False`` on every ``json.dumps``** so non-ASCII survives raw
  (the Node ``JSON.stringify`` emits raw UTF-8).

Parity notes:

* **Nullish, not falsy.** Every TS ``??`` / ``=== undefined`` / ``!== undefined``
  ports as ``.get(k, default)`` / ``is None`` / ``is not None`` so an explicit
  falsy value (``0`` / ``False`` / ``""`` / ``[]``) survives. Model precedence in
  ``handle_start_phase`` / ``handle_run_status`` keys on non-empty-string presence
  exactly as the TS ``typeof x === 'string' && x.length > 0 ? x : undefined``
  chain.
* **``complete_phase`` ordering.** The gate is evaluated BEFORE the completion
  transition: one ``gate_evaluated`` event is emitted per check (so the audit
  trail records the block reason even on rollback), then on a blocking failure
  the phase artifacts are rolled back via ``remove_phase_artifacts`` and a
  ``gate_blocked`` ``PipelineError`` is raised. On pass (or warn), the phase is
  completed, then ``post_complete`` hooks fire BEFORE ``append_phase_completed_event``
  writes the ``phase_completed`` line — the hooks.ts idempotency guard compares
  against events.jsonl, so writing the event first would trip the guard and skip
  every legitimate hook.
* **Diagnostics → stderr.** ``append_phase_completed_event``'s duplicate-suppression
  warning goes to stderr (the Node ``console.warn``); nothing writes to stdout.

:func:`register_lifecycle_tools` registers the 11 ``pipeline_*`` lifecycle tools
(``get_config`` / ``init_run`` / ``next_phases`` / ``start_phase`` /
``complete_phase`` / ``fail_phase`` / ``retry_phase`` / ``run_status`` /
``reload_state`` / ``phase_handoff`` / ``phase_brief``) with their per-tool
``_meta.max_result_chars`` (50000 read / 10000 mutate) + ``ToolAnnotations``
subset from the frozen ``tool-schemas.ts`` map. It is NOT called from anywhere
yet: the global ``register_tools`` seam (``tools/__init__.py``) stays a no-op
until the wiring task (T6.5), so the global handshake enumerates 0 tools.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from mcp.types import ToolAnnotations

from pipeline_orchestrator.dag import InputSatisfaction
from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.handoff import generate_handoff
from pipeline_orchestrator.models import (
    ArtifactRef,
    HandlerResponse,
    PhaseDefinition,
    PipelineConfig,
    QualityGate,
    RunState,
)
from pipeline_orchestrator.phase_brief import generate_phase_brief
from pipeline_orchestrator.quality_gates import GateResult
from pipeline_orchestrator.run_state import (
    RecommendedAction,
    RecoveryResult,
    compute_run_terminal_status,
    finalize_run_terminal_status,
    get_artifact_types,
    get_completed_phases,
    get_skipped_phases,
)
from pipeline_orchestrator.tools._envelope import ResponseEnvelope

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.hooks import PreInitHookResult


# ── Context provided by server.py (the DI seam) ──────────────────────


@dataclass
class LifecycleContext:
    """Dependency object the lifecycle handlers call through (TS ``LifecycleContext``).

    ``server.py`` (T6.5) wires every callable to the real ``ServerState`` seams /
    ``run_state`` / ``dag`` / ``quality_gates`` / ``hooks`` functions; tests inject
    stubs that capture local state (mirroring the TS ``makeCtx`` factory).
    """

    get_config: Callable[[], PipelineConfig]
    set_project_root: Callable[[str], None]
    get_storage_config: Callable[..., Any]
    get_active_run: Callable[[], RunState | None]
    set_active_run: Callable[[RunState], None]
    get_active_run_dir: Callable[[], str]
    set_active_run_dir: Callable[[str], None]
    init_run: Callable[..., RunState]
    skip_phase: Callable[[RunState, str, str], RunState]
    add_artifact: Callable[[RunState, ArtifactRef, str], RunState]
    start_phase: Callable[[RunState, str, str], RunState]
    complete_phase: Callable[[RunState, str, str], RunState]
    fail_phase: Callable[[RunState, str, str, str], RunState]
    retry_phase: Callable[[RunState, str, str], RunState]
    resolve_next_phases: Callable[
        [PipelineConfig, list[str], list[str], list[str]], list[str]
    ]
    get_phase_input_satisfaction: Callable[
        [PhaseDefinition, list[str]], InputSatisfaction
    ]
    load_run_state: Callable[[str], RunState | None]
    recover_run: Callable[[str], RecoveryResult]
    remove_phase_artifacts: Callable[[RunState, str, str], list[str]]
    get_quality_gates: Callable[[], list[QualityGate]]
    run_gate_checks: Callable[[QualityGate, list[ArtifactRef]], GateResult]
    get_hooks_config: Callable[[], list[Any]]
    run_hooks: Callable[
        [list[Any], str, dict[str, Any], str], list[dict[str, Any]]
    ]
    run_pre_pipeline_init_hooks: Callable[
        [list[Any], dict[str, Any], str], PreInitHookResult
    ]
    get_project_root: Callable[[], str | None]
    compute_recommended_action: Callable[
        [RunState, list[str]], RecommendedAction
    ]
    compute_run_warnings: Callable[..., list[str]]


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (Node ``toISOString()``)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _recommended_action_to_dict(action: RecommendedAction) -> dict[str, Any]:
    """Serialize a :class:`RecommendedAction`, omitting an absent ``phase``.

    Mirrors the Node ``RecommendedAction`` object literal: ``phase`` is the ``?:``
    optional and is dropped (``undefined``) when ``None``. Key order: ``action``,
    ``phase`` (when present), ``reason``.
    """
    out: dict[str, Any] = {"action": action.action}
    if action.phase is not None:
        out["phase"] = action.phase
    out["reason"] = action.reason
    return out


def _artifact_to_dict(ref: ArtifactRef) -> dict[str, Any]:
    """Serialize an :class:`ArtifactRef` for the ``run_status`` JSON, omitting None.

    Mirrors the Node object-literal serialization: ``version`` / ``parent_artifact``
    are the ``?:`` optionals and are dropped when ``None``. Key order: ``type``,
    ``path``, ``phase``, ``created_at``, ``version`` (when present),
    ``parent_artifact`` (when present).
    """
    out: dict[str, Any] = {
        "type": ref.type,
        "path": ref.path,
        "phase": ref.phase,
        "created_at": ref.created_at,
    }
    if ref.version is not None:
        out["version"] = ref.version
    if ref.parent_artifact is not None:
        out["parent_artifact"] = ref.parent_artifact
    return out


# ── Events log ──────────────────────────────────────────────────────


def append_event(run_dir: str, event: dict[str, Any]) -> None:
    """Append a lifecycle event to ``events.jsonl`` in the run directory.

    Creates the directory if it does not exist. Each event is one
    ``json.dumps(event, separators=(",", ":"))`` line (single-line, matching the
    Node ``JSON.stringify(event)``). Mirrors ``appendEvent`` in
    ``lifecycle-handlers.ts``.
    """
    if not os.path.exists(run_dir):
        os.makedirs(run_dir, exist_ok=True)
    events_path = os.path.join(run_dir, "events.jsonl")
    line = json.dumps(event, separators=(",", ":"), ensure_ascii=False)
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def append_phase_completed_event(
    run_dir: str,
    state: RunState,
    phase: str,
) -> bool:
    """Write a validated, on-disk-idempotent ``phase_completed`` event.

    Mirrors ``appendPhaseCompletedEvent`` in ``lifecycle-handlers.ts``. Callers
    MUST go through this wrapper for ``phase_completed`` events; raw
    :func:`append_event` calls are reserved for other event types.

    Behavior:

    * Raises ``PipelineError(state_transition_error)`` (byte-exact message) if
      ``phase`` is not in ``state.phases``, or if its status is not
      ``'completed'``.
    * If a ``phase_completed`` event for ``phase`` already exists in
      ``{run_dir}/events.jsonl``, logs a warning to stderr and returns ``False``
      WITHOUT writing.
    * Otherwise writes the standard event and returns ``True``.
    """
    phase_state = state.phases.get(phase)
    if phase_state is None:
        raise PipelineError(
            f'cannot append phase_completed event: phase "{phase}" '
            "is not in run state",
            ErrorClass.state_transition_error,
            recovery_action=(
                "Verify phase name exists in the run state before completing."
            ),
            details={"phase": phase, "known_phases": list(state.phases.keys())},
        )
    if phase_state.status != "completed":
        raise PipelineError(
            f'cannot append phase_completed event: phase "{phase}" status is '
            f'"{phase_state.status}", expected completed',
            ErrorClass.state_transition_error,
            recovery_action=(
                "Transition the phase to completed before writing a "
                "phase_completed event."
            ),
            details={
                "phase": phase,
                "current_status": phase_state.status,
                "expected_status": "completed",
            },
        )

    # On-disk duplicate suppression — defense-in-depth against late-firing callers.
    try:
        events_path = os.path.join(run_dir, "events.jsonl")
        if os.path.exists(events_path):
            with open(events_path, encoding="utf-8") as fh:
                content = fh.read()
            for line in content.split("\n"):
                trimmed = line.strip()
                if not trimmed:
                    continue
                try:
                    parsed = json.loads(trimmed)
                except (json.JSONDecodeError, ValueError):
                    # Malformed line — skip defensively.
                    continue
                if (
                    isinstance(parsed, dict)
                    and parsed.get("event") == "phase_completed"
                    and parsed.get("phase") == phase
                ):
                    timestamp = _now_iso()
                    print(
                        "[hooks] suppressed duplicate phase_completed write for "
                        f'phase="{phase}" at {timestamp}',
                        file=sys.stderr,
                    )
                    return False
    except OSError:
        # If we can't read the file for any reason, fall through and write.
        pass

    append_event(
        run_dir,
        {
            "timestamp": _now_iso(),
            "event": "phase_completed",
            "phase": phase,
            "run_id": state.run_id,
        },
    )
    return True


# ── Helpers ──────────────────────────────────────────────────────────


def detect_stale_state(ctx: LifecycleContext) -> RunState | None:
    """Return the disk state if it has diverged from the in-memory active run.

    Mirrors ``detectStaleState`` in ``lifecycle-handlers.ts``: returns the disk
    state when ``disk.updated_at != active_run.updated_at``, else ``None``.
    """
    active_run = ctx.get_active_run()
    active_run_dir = ctx.get_active_run_dir()
    if not active_run or not active_run_dir:
        return None
    disk = ctx.load_run_state(active_run_dir)
    if not disk:
        return None
    if disk.updated_at != active_run.updated_at:
        return disk
    return None


def reload_state_from_disk(ctx: LifecycleContext) -> dict[str, Any]:
    """Reload run state from disk, replacing the in-memory active run.

    Mirrors ``reloadStateFromDisk`` in ``lifecycle-handlers.ts``. Returns a dict
    ``{"reloaded": RunState, "changes": list[str]}`` describing what changed.
    """
    active_run_dir = ctx.get_active_run_dir()
    if not active_run_dir:
        raise ValueError("No active run directory")

    disk = ctx.load_run_state(active_run_dir)
    if not disk:
        raise ValueError("No run-state.json found on disk")

    changes: list[str] = []
    active_run = ctx.get_active_run()
    if active_run:
        # Artifact count diff
        old_count = len(active_run.available_artifacts)
        new_count = len(disk.available_artifacts)
        if new_count != old_count:
            changes.append(f"artifacts: {old_count} → {new_count}")

        # Phase status changes
        for name, disk_phase in disk.phases.items():
            mem_phase = active_run.phases.get(name)
            if mem_phase and mem_phase.status != disk_phase.status:
                changes.append(
                    f'phase "{name}": {mem_phase.status} → '
                    f"{disk_phase.status}"
                )
            elif not mem_phase:
                changes.append(f'phase "{name}": new ({disk_phase.status})')

        # Overall status
        if active_run.status != disk.status:
            changes.append(
                f"run status: {active_run.status} → {disk.status}"
            )

        if len(changes) == 0:
            changes.append(
                "updated_at changed but no structural differences detected"
            )
    else:
        changes.append("no previous in-memory state; loaded from disk")

    ctx.set_active_run(disk)
    return {"reloaded": disk, "changes": changes}


# ── Handlers ─────────────────────────────────────────────────────────


def handle_init_run(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Initialize a new pipeline run (port of TS ``handleInitRun``).

    Runs ``pre_pipeline_init`` hooks, short-circuiting with a
    ``user_input_required`` envelope when a hook needs input and the caller has
    not supplied ``run_parameters``. Otherwise merges hook params with
    caller-supplied params (user wins), inits the run, redirects ``active_run_dir``
    to the canonical per-run tree (Feature C step C8) when ``run_data_dir`` was
    computed, skips listed phases, registers initial artifacts, and returns the
    run summary.
    """
    run_id = args.get("run_id")
    if not run_id:
        raise ValueError("run_id is required")
    assert isinstance(run_id, str)

    project_root = args.get("project_root")
    if not project_root:
        raise ValueError(
            "project_root is required — absolute path to the project directory"
        )
    assert isinstance(project_root, str)
    ctx.set_project_root(project_root)

    base_dir = args.get("base_dir")
    skip_phases_arg = args.get("skip_phases")
    skip_phases_list: list[str] = (
        skip_phases_arg if isinstance(skip_phases_arg, list) else []
    )
    initial_artifacts_arg = args.get("initial_artifacts")
    initial_artifacts: list[dict[str, Any]] = (
        initial_artifacts_arg if isinstance(initial_artifacts_arg, list) else []
    )

    config = ctx.get_config()
    sc = ctx.get_storage_config(base_dir)
    legacy_run_dir = os.path.join(sc.base_dir, "runs", run_id)
    ctx.set_active_run_dir(legacy_run_dir)
    phase_names = [p for p in config.phases if p not in skip_phases_list]

    # Collect run parameters from pre_pipeline_init hooks (if any are registered).
    run_parameters: dict[str, Any] = {}
    hooks = ctx.get_hooks_config()
    pre_init_hooks = [h for h in hooks if h.trigger == "pre_pipeline_init"]
    user_supplied_params = args.get("run_parameters")
    has_user_params = user_supplied_params is not None

    if len(pre_init_hooks) > 0:
        pre_init_context: dict[str, Any] = {
            "trigger": "pre_pipeline_init",
            "project_root": project_root,
            "requested_args": args,
        }
        hook_result = ctx.run_pre_pipeline_init_hooks(
            hooks, pre_init_context, project_root
        )

        # Short-circuit when the hook needs user input and the caller hasn't
        # provided it yet.
        if len(hook_result.userPrompts) > 0 and not has_user_params:
            envelope = ResponseEnvelope(
                status="ok",
                data={
                    "status": "user_input_required",
                    "prompts": hook_result.userPrompts,
                    "partial_parameters": hook_result.parameters,
                },
                next_step=(
                    "Collect answers to the prompts above from the user and "
                    "re-invoke pipeline_init_run with a run_parameters object "
                    "containing the answers."
                ),
            )
            return HandlerResponse(json=envelope.to_json())

        # Merge hook parameters with caller-supplied run_parameters.
        # User-supplied values take precedence over hook defaults.
        merged: dict[str, Any] = dict(hook_result.parameters)
        if has_user_params:
            assert isinstance(user_supplied_params, dict)
            merged.update(user_supplied_params)
        run_parameters = merged
    elif has_user_params:
        assert isinstance(user_supplied_params, dict)
        run_parameters = user_supplied_params

    active_run = ctx.init_run(
        run_id,
        config.pipeline.version,
        phase_names,
        legacy_run_dir,
        run_parameters,
    )

    # Feature C step C8: redirect activeRunDir to the canonical per-run tree
    # once initRun has computed run_data_dir. Legacy runs keep legacyRunDir.
    active_run_dir = (
        active_run.run_data_dir
        if active_run.run_data_dir is not None
        else legacy_run_dir
    )
    if active_run_dir != legacy_run_dir:
        ctx.set_active_run_dir(active_run_dir)

    # Skip explicitly listed phases
    for phase in skip_phases_list:
        if active_run.phases.get(phase):
            active_run = ctx.skip_phase(active_run, phase, active_run_dir)

    # Register initial artifacts
    for art in initial_artifacts:
        active_run = ctx.add_artifact(
            active_run,
            ArtifactRef(
                type=art["type"],
                path=art["path"],
                phase="pre-existing",
                created_at=_now_iso(),
                version=1,
            ),
            active_run_dir,
        )

    ctx.set_active_run(active_run)

    artifact_types = get_artifact_types(active_run)
    completed_phases = get_completed_phases(active_run)

    next_phases = ctx.resolve_next_phases(
        config, completed_phases, artifact_types, skip_phases_list
    )

    return HandlerResponse(
        json=json.dumps(
            {
                "run_id": active_run.run_id,
                "status": active_run.status,
                "total_phases": len(active_run.phases),
                "skipped": skip_phases_list,
                "initial_artifacts": artifact_types,
                "available_next_phases": next_phases,
                "run_parameters": (
                    active_run.run_parameters
                    if active_run.run_parameters is not None
                    else {}
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def handle_next_phases(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Resolve which phases can run next (port of TS ``handleNextPhases``).

    Auto-reloads when disk state has diverged, then returns the candidate phases
    with per-phase input satisfaction, the completed-phase list, and the
    available artifact types.
    """
    active_run = ctx.get_active_run()
    if not active_run:
        raise ValueError("No active run. Call pipeline_init_run first.")

    extra_skips_arg = args.get("skip_phases")
    extra_skips: list[str] = (
        extra_skips_arg if isinstance(extra_skips_arg, list) else []
    )

    # Auto-reload if disk state has diverged
    stale_note: str | None = None
    stale = detect_stale_state(ctx)
    if stale:
        changes = reload_state_from_disk(ctx)["changes"]
        stale_note = f"State auto-reloaded from disk ({'; '.join(changes)})"
    state = ctx.get_active_run()
    assert state is not None

    config = ctx.get_config()
    completed = get_completed_phases(state)
    artifact_types = get_artifact_types(state)
    all_skips = [*get_skipped_phases(state), *extra_skips]

    next_phases = ctx.resolve_next_phases(
        config, completed, artifact_types, all_skips
    )

    details: list[dict[str, Any]] = []
    for name in next_phases:
        phase = config.phases[name]
        sat = ctx.get_phase_input_satisfaction(phase, artifact_types)
        details.append(
            {
                "name": name,
                "inputs_satisfied": sat.satisfied,
                "inputs_missing": sat.missing,
                "description": phase.description,
                "entry_point": phase.entry_point,
            }
        )

    result: dict[str, Any] = {
        "available_phases": details,
        "completed": completed,
        "artifacts": artifact_types,
    }
    if stale_note:
        result["state_reloaded"] = stale_note

    return HandlerResponse(
        json=json.dumps(result, indent=2, ensure_ascii=False)
    )


def _resolve_model(
    phase_def: PhaseDefinition | None,
    phase_model_param: Any,
) -> str:
    """Resolve the concrete model ID via the TS precedence chain (Feature B).

    Precedence (non-empty-string presence, not truthiness):
      1. ``phase_def.model`` (phase-specific, highest)
      2. ``run_parameters.phase_model`` (run default)
      3. ``'claude-sonnet-4-6'`` (fallback)
    """
    phase_def_model = (
        phase_def.model
        if (
            phase_def is not None
            and isinstance(phase_def.model, str)
            and len(phase_def.model) > 0
        )
        else None
    )
    run_param_model = (
        phase_model_param
        if (
            isinstance(phase_model_param, str) and len(phase_model_param) > 0
        )
        else None
    )
    if phase_def_model is not None:
        return phase_def_model
    if run_param_model is not None:
        return run_param_model
    return "claude-sonnet-4-6"


def handle_start_phase(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Mark a phase as in-progress (port of TS ``handleStartPhase``).

    Resolves upstream input artifacts onto the phase, resolves the model ID via
    the precedence chain, generates the phase brief, fires blocking ``pre_start``
    hooks (collecting the first ``agent_directive``), transitions the phase,
    appends a ``phase_started`` event (with ``resolved_model``), and returns the
    phase definition.
    """
    state = ctx.get_active_run()
    if not state:
        raise ValueError("No active run. Call pipeline_init_run first.")

    phase_name = args.get("phase")
    if not phase_name:
        raise ValueError("phase is required")
    assert isinstance(phase_name, str)

    config = ctx.get_config()
    active_run_dir = ctx.get_active_run_dir()

    # Resolve upstream input artifacts from the DAG and record them on the phase.
    from pipeline_orchestrator.dag import resolve_input_artifacts

    input_artifacts = resolve_input_artifacts(state, phase_name, config)
    phase_state_slot = state.phases.get(phase_name)
    if phase_state_slot:
        phase_state_slot.input_artifacts = input_artifacts

    # Feature B: resolve the concrete Claude model ID via the precedence chain.
    phase_def = config.phases.get(phase_name)
    phase_model_param = (
        state.run_parameters.get("phase_model")
        if state.run_parameters is not None
        else None
    )
    resolved_model = _resolve_model(phase_def, phase_model_param)
    run_data_dir = (
        state.run_data_dir
        if state.run_data_dir is not None
        else active_run_dir
    )

    # Generate the phase brief so the pre_start hook can embed it.
    phase_brief = generate_phase_brief(
        phase_name,
        state,
        config,
        ctx.get_quality_gates(),
        resolved_model=resolved_model,
        run_data_dir=run_data_dir,
    )

    # Run pre_start hooks (blocking: failure throws PipelineError). Collect the
    # first agent_directive from any matching hook.
    agent_directive: dict[str, Any] | None = None
    project_root = ctx.get_project_root()
    hooks = ctx.get_hooks_config()
    if project_root and len(hooks) > 0:
        hook_context: dict[str, Any] = {
            "run_id": state.run_id,
            "phase": phase_name,
            "trigger": "pre_start",
            "project_root": project_root,
            "run_dir": active_run_dir,
            "run_parameters": (
                state.run_parameters
                if state.run_parameters is not None
                else {}
            ),
            "phase_brief": phase_brief,
            "resolved_model": resolved_model,
        }
        hook_results = ctx.run_hooks(
            hooks, "pre_start", hook_context, project_root
        )
        for hr in hook_results:
            if hr.get("agent_directive"):
                agent_directive = hr["agent_directive"]
                break

    updated = ctx.start_phase(state, phase_name, active_run_dir)
    ctx.set_active_run(updated)

    # Log phase_started event (resolved_model included for cost profiling — M5.2)
    append_event(
        active_run_dir,
        {
            "timestamp": _now_iso(),
            "event": "phase_started",
            "phase": phase_name,
            "run_id": updated.run_id,
            "resolved_model": resolved_model,
        },
    )

    phase = config.phases[phase_name]

    response_body: dict[str, Any] = {
        "phase": phase_name,
        "status": "in_progress",
        "description": phase.description,
        "inputs": phase.inputs,
        "input_mode": phase.input_mode,
        "outputs": phase.outputs,
        "tools": phase.tools,
        "input_artifacts": input_artifacts,
        "resolved_model": resolved_model,
    }
    if agent_directive:
        response_body["agent_directive"] = agent_directive

    return HandlerResponse(
        json=json.dumps(response_body, indent=2, ensure_ascii=False)
    )


def handle_complete_phase(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Complete a phase (port of TS ``handleCompletePhase``).

    The big one. State-level idempotency guard (no-op envelope on a re-call);
    quality-gate evaluation emitting one ``gate_evaluated`` event per check BEFORE
    any block decision; atomic rollback + ``gate_blocked`` raise on a blocking
    failure (warn-mode collects warnings); the completion transition; then
    ``post_complete`` hooks fire BEFORE ``append_phase_completed_event`` writes the
    ``phase_completed`` line (the hooks idempotency guard reads events.jsonl);
    then run-terminal-status re-evaluation; then the success envelope.
    """
    state = ctx.get_active_run()
    if not state:
        raise ValueError("No active run. Call pipeline_init_run first.")

    phase_name = args.get("phase")
    if not phase_name:
        raise ValueError("phase is required")
    assert isinstance(phase_name, str)

    # State-level idempotency guard: if already completed, return a no-op
    # envelope with a warning. Do NOT re-run gates / events / hooks.
    existing_phase = state.phases.get(phase_name)
    if existing_phase is not None and existing_phase.status == "completed":
        envelope = ResponseEnvelope(
            status="ok",
            data={
                "phase": phase_name,
                "completed_at": existing_phase.completed_at,
                "run_status": state.status,
            },
            next_step=(
                "Phase already completed. Call pipeline_next_phases to continue."
            ),
            warnings=[
                "phase already completed — ignoring duplicate "
                f'pipeline_complete_phase call for phase "{phase_name}"'
            ],
        )
        return HandlerResponse(json=envelope.to_json())

    # Run quality gate checks before completing
    gates = ctx.get_quality_gates()
    gate = next((g for g in gates if g.phase == phase_name), None)
    gate_warnings: list[str] = []

    if gate is not None:
        phase_artifacts = [
            a for a in state.available_artifacts if a.phase == phase_name
        ]
        gate_result = ctx.run_gate_checks(gate, phase_artifacts)

        # Emit one gate_evaluated event per check BEFORE any block decision.
        # gate.checks and gate_result.results are aligned by index.
        gate_run_dir = ctx.get_active_run_dir()
        for i in range(len(gate_result.results)):
            check_result = gate_result.results[i]
            check_config = gate.checks[i] if i < len(gate.checks) else None
            artifact_type_param = (
                check_config.params.get("artifact_type")
                if check_config is not None
                else None
            )
            resolved_artifact_type = (
                artifact_type_param
                if artifact_type_param is not None
                else (
                    phase_artifacts[0].type
                    if len(phase_artifacts) > 0
                    else None
                )
            )
            if check_result.passed:
                result = "pass"
            elif gate.on_failure == "block":
                result = "fail"
            else:
                result = "warn"
            details: dict[str, Any] = {
                "gate_name": gate.phase,
                "check_type": check_result.check_type,
                "result": result,
                "message": check_result.message,
            }
            if resolved_artifact_type is not None:
                details["artifact_type"] = resolved_artifact_type
            append_event(
                gate_run_dir,
                {
                    "timestamp": _now_iso(),
                    "event": "gate_evaluated",
                    "phase": phase_name,
                    "run_id": state.run_id,
                    "details": details,
                },
            )

        if not gate_result.passed:
            if gate.on_failure == "block":
                # Atomic rollback: revert phase artifacts before raising.
                active_run_dir = ctx.get_active_run_dir()
                ctx.remove_phase_artifacts(state, phase_name, active_run_dir)

                failed_checks = [r for r in gate_result.results if not r.passed]
                raise PipelineError(
                    f'Quality gate blocked completion of phase "{phase_name}": '
                    + "; ".join(c.message for c in failed_checks),
                    ErrorClass.gate_blocked,
                    recovery_action=(
                        "Fix the failing quality checks and retry "
                        "pipeline_complete_phase."
                    ),
                    details={
                        "phase": phase_name,
                        "gate_result": _gate_result_to_dict(gate_result),
                        "removed_artifacts": [
                            a.path
                            for a in state.available_artifacts
                            if a.phase == phase_name
                        ],
                    },
                )
            # on_failure == 'warn': proceed but capture warnings.
            failed_checks = [r for r in gate_result.results if not r.passed]
            gate_warnings.extend(
                f"Quality gate warning: {c.message}" for c in failed_checks
            )

    config = ctx.get_config()
    active_run_dir = ctx.get_active_run_dir()
    updated = ctx.complete_phase(state, phase_name, active_run_dir)
    ctx.set_active_run(updated)

    # Order: run post_complete hooks BEFORE appending phase_completed. The
    # hooks idempotency guard compares against events.jsonl; writing the event
    # first would trip it and skip every legitimate hook.
    hook_warnings: list[str] = []
    project_root = ctx.get_project_root()
    hooks = ctx.get_hooks_config()
    if project_root and len(hooks) > 0:
        hook_context: dict[str, Any] = {
            "run_id": updated.run_id,
            "phase": phase_name,
            "trigger": "post_complete",
            "project_root": project_root,
            "run_dir": active_run_dir,
        }
        hook_results = ctx.run_hooks(
            hooks, "post_complete", hook_context, project_root
        )
        for hr in hook_results:
            if not hr.get("success"):
                hook_warnings.append(
                    f"Post-complete hook warning: {hr.get('command')} — "
                    f"{hr.get('error')}"
                )

    # Log phase_completed event via the validated wrapper.
    append_phase_completed_event(active_run_dir, updated, phase_name)

    # Re-evaluate whether the run as a whole has reached a terminal state.
    terminal = compute_run_terminal_status(updated, config)
    final_state = updated
    if (
        terminal in ("completed", "failed")
        and updated.status != terminal
    ):
        final_state = finalize_run_terminal_status(
            updated, terminal, active_run_dir
        )
        ctx.set_active_run(final_state)

    completed = get_completed_phases(final_state)
    artifact_types = get_artifact_types(final_state)
    skips = get_skipped_phases(final_state)
    next_phases = ctx.resolve_next_phases(
        config, completed, artifact_types, skips
    )

    all_warnings = [*gate_warnings, *hook_warnings]

    envelope = ResponseEnvelope(
        status="ok",
        data={
            "phase": phase_name,
            "completed_at": final_state.phases[phase_name].completed_at,
            "run_status": final_state.status,
        },
        next_step=(
            f"Available phases: {', '.join(next_phases)}. "
            "Call pipeline_start_phase to begin."
            if len(next_phases) > 0
            else (
                "All phases complete. Call pipeline_validate_run for final "
                "validation."
            )
        ),
        warnings=all_warnings if len(all_warnings) > 0 else None,
    )
    return HandlerResponse(json=envelope.to_json())


def _gate_result_to_dict(gate_result: GateResult) -> dict[str, Any]:
    """Serialize a :class:`GateResult` for the ``gate_blocked`` error details.

    Mirrors the Node ``GateResult`` object literal: ``phase``, ``passed``,
    ``on_failure``, ``results`` (each ``{check_type, passed, message}``).
    """
    return {
        "phase": gate_result.phase,
        "passed": gate_result.passed,
        "on_failure": gate_result.on_failure,
        "results": [
            {
                "check_type": r.check_type,
                "passed": r.passed,
                "message": r.message,
            }
            for r in gate_result.results
        ],
    }


def handle_fail_phase(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Mark a phase as failed (port of TS ``handleFailPhase``).

    Transitions the phase, appends a ``phase_failed`` event, fires non-blocking
    ``on_fail`` hooks, and returns an error envelope whose ``next_step`` branches
    on whether the phase is optional.
    """
    state = ctx.get_active_run()
    if not state:
        raise ValueError("No active run. Call pipeline_init_run first.")

    phase_name = args.get("phase")
    error = args.get("error")
    if not phase_name:
        raise ValueError("phase is required")
    if not error:
        raise ValueError("error is required")
    assert isinstance(phase_name, str)
    assert isinstance(error, str)

    config = ctx.get_config()
    active_run_dir = ctx.get_active_run_dir()
    updated = ctx.fail_phase(state, phase_name, error, active_run_dir)
    ctx.set_active_run(updated)

    # Log phase_failed event
    append_event(
        active_run_dir,
        {
            "timestamp": _now_iso(),
            "event": "phase_failed",
            "phase": phase_name,
            "run_id": updated.run_id,
            "details": {"error": error},
        },
    )

    # Run on_fail hooks (non-blocking: failures are warnings)
    project_root = ctx.get_project_root()
    hooks = ctx.get_hooks_config()
    hook_warnings: list[str] = []
    if project_root and len(hooks) > 0:
        hook_context: dict[str, Any] = {
            "run_id": updated.run_id,
            "phase": phase_name,
            "trigger": "on_fail",
            "project_root": project_root,
            "run_dir": active_run_dir,
            "error": error,
        }
        hook_results = ctx.run_hooks(hooks, "on_fail", hook_context, project_root)
        for hr in hook_results:
            if not hr.get("success"):
                hook_warnings.append(
                    f"On-fail hook warning: {hr.get('command')} — "
                    f"{hr.get('error')}"
                )

    phase_def = config.phases.get(phase_name)
    is_optional = phase_def is not None and phase_def.optional is True
    envelope = ResponseEnvelope(
        status="error",
        data={"phase": phase_name, "error": error},
        next_step=(
            f'Phase "{phase_name}" is optional. Call pipeline_next_phases to '
            "skip and continue."
            if is_optional
            else (
                f'Phase "{phase_name}" failed. Fix the underlying issue and '
                "call pipeline_retry_phase to retry."
            )
        ),
        warnings=hook_warnings if len(hook_warnings) > 0 else None,
    )
    return HandlerResponse(json=envelope.to_json())


def handle_retry_phase(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Retry a failed phase (port of TS ``handleRetryPhase``).

    Transitions the failed phase back to pending (incrementing ``retry_count``)
    and returns an ``ok`` envelope.
    """
    state = ctx.get_active_run()
    if not state:
        raise ValueError("No active run. Call pipeline_init_run first.")

    phase_name = args.get("phase")
    if not phase_name:
        raise ValueError("phase is required")
    assert isinstance(phase_name, str)

    active_run_dir = ctx.get_active_run_dir()
    updated = ctx.retry_phase(state, phase_name, active_run_dir)
    ctx.set_active_run(updated)

    retry_count = updated.phases[phase_name].retry_count

    envelope = ResponseEnvelope(
        status="ok",
        data={
            "phase": phase_name,
            "status": "pending",
            "retry_count": retry_count,
        },
        next_step="Call pipeline_start_phase to re-execute the phase.",
    )
    return HandlerResponse(json=envelope.to_json())


def handle_run_status(ctx: LifecycleContext) -> HandlerResponse:
    """Get the current run state (port of TS ``handleRunStatus``).

    Returns a ``no_run`` shape when there is no active run. Otherwise auto-reloads
    on disk divergence, builds the per-phase summary + artifacts + recommended
    action + warnings, and surfaces ``current_phase_resolved_model`` for the
    in-progress phase.
    """
    active_run = ctx.get_active_run()
    if not active_run:
        return HandlerResponse(
            json=json.dumps(
                {
                    "status": "no_run",
                    "recommended_action": {
                        "action": "init",
                        "reason": (
                            "No active run. Call pipeline_init_run to start."
                        ),
                    },
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    # Auto-reload if disk state has diverged
    stale_note: str | None = None
    stale = detect_stale_state(ctx)
    if stale:
        changes = reload_state_from_disk(ctx)["changes"]
        stale_note = f"State auto-reloaded from disk ({'; '.join(changes)})"
    state = ctx.get_active_run()
    assert state is not None

    config = ctx.get_config()
    summary: list[dict[str, Any]] = []
    for name, p in state.phases.items():
        summary.append(
            {
                "phase": name,
                "status": p.status,
                "started": p.started_at if p.started_at is not None else None,
                "completed": (
                    p.completed_at if p.completed_at is not None else None
                ),
                "error": p.error if p.error is not None else None,
                "outputs": p.output_artifacts,
            }
        )

    # Compute recommended action
    completed = get_completed_phases(state)
    artifact_types = get_artifact_types(state)
    skips = get_skipped_phases(state)
    next_phases = ctx.resolve_next_phases(
        config, completed, artifact_types, skips
    )

    optional_phases = {
        name
        for name, p in config.phases.items()
        if p.optional is True
    }
    warnings = ctx.compute_run_warnings(state, optional_phases)
    recommended_action = ctx.compute_recommended_action(state, next_phases)

    result: dict[str, Any] = {
        "run_id": state.run_id,
        "status": state.status,
        "created": state.created_at,
        "updated": state.updated_at,
        "phases": summary,
        "artifacts": [
            _artifact_to_dict(a) for a in state.available_artifacts
        ],
        "recommended_action": _recommended_action_to_dict(recommended_action),
    }
    # Expose resolved_model for the current in-progress phase (cost auditing)
    in_progress_phase = next(
        (
            (name, p)
            for name, p in state.phases.items()
            if p.status == "in_progress"
        ),
        None,
    )
    if in_progress_phase is not None:
        in_progress_name = in_progress_phase[0]
        phase_def = config.phases.get(in_progress_name)
        phase_model_param = (
            state.run_parameters.get("phase_model")
            if state.run_parameters is not None
            else None
        )
        result["current_phase_resolved_model"] = _resolve_model(
            phase_def, phase_model_param
        )

    if len(warnings) > 0:
        result["warnings"] = warnings
    if stale_note:
        result["state_reloaded"] = stale_note

    return HandlerResponse(
        json=json.dumps(result, indent=2, ensure_ascii=False)
    )


def handle_reload_state(ctx: LifecycleContext) -> HandlerResponse:
    """Reload the active run state from disk (port of TS ``handleReloadState``).

    Uses ``recover_run`` for full recovery (integrity check + in_progress reset)
    and returns a backward-compatible changes list.
    """
    active_run_dir = ctx.get_active_run_dir()
    if not active_run_dir:
        raise ValueError("No active run. Call pipeline_init_run first.")

    # Use recoverRun for full recovery (integrity check + in_progress reset)
    recovery = ctx.recover_run(active_run_dir)
    state = recovery.state
    recovered_phases = recovery.recovered_phases
    warnings = recovery.warnings
    ctx.set_active_run(state)

    # Build changes list from recovery for backward compatibility
    changes: list[str] = []
    if len(recovered_phases) > 0:
        changes.append(f"recovered phases: {', '.join(recovered_phases)}")
    if len(warnings) > 0:
        changes.extend(warnings)
    if len(changes) == 0:
        changes.append("state reloaded from disk — no recovery needed")

    return HandlerResponse(
        json=json.dumps(
            {
                "run_id": state.run_id,
                "status": state.status,
                "updated_at": state.updated_at,
                "artifact_count": len(state.available_artifacts),
                "recovered_phases": recovered_phases,
                "changes": changes,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def handle_phase_handoff(ctx: LifecycleContext) -> HandlerResponse:
    """Generate a compact handoff payload (port of TS ``handlePhaseHandoff``)."""
    state = ctx.get_active_run()
    if not state:
        raise ValueError("No active run. Call pipeline_init_run first.")

    config = ctx.get_config()
    completed = get_completed_phases(state)
    artifact_types = get_artifact_types(state)
    skips = get_skipped_phases(state)
    next_phases = ctx.resolve_next_phases(
        config, completed, artifact_types, skips
    )
    handoff = generate_handoff(state, config, ctx.get_quality_gates(), next_phases)

    return HandlerResponse(
        json=json.dumps(handoff, indent=2, ensure_ascii=False)
    )


def handle_phase_brief(
    args: dict[str, Any], ctx: LifecycleContext
) -> HandlerResponse:
    """Generate a self-contained subagent brief (port of TS ``handlePhaseBrief``)."""
    state = ctx.get_active_run()
    if not state:
        raise ValueError("No active run. Call pipeline_init_run first.")

    phase_name = args.get("phase")
    if not phase_name:
        raise ValueError("phase is required")
    assert isinstance(phase_name, str)

    config = ctx.get_config()
    brief = generate_phase_brief(
        phase_name, state, config, ctx.get_quality_gates()
    )

    return HandlerResponse(
        json=json.dumps(brief, indent=2, ensure_ascii=False)
    )


# ── Tool registration (NOT wired into the global seam yet — T6.5) ────

_READ_MAX = 50000
_MUTATE_MAX = 10000


def register_lifecycle_tools(mcp: FastMCP) -> None:
    """Register the 11 lifecycle tools onto ``mcp`` (spec §5, frozen annotations).

    Per-tool ``_meta.max_result_chars`` + ``ToolAnnotations`` come from the frozen
    ``tool-schemas.ts`` map (byte-exact): read tools
    (``get_config`` / ``next_phases`` / ``run_status`` / ``reload_state`` /
    ``phase_handoff`` / ``phase_brief``) carry ``readOnlyHint=True`` + 50000;
    mutating tools (``init_run`` / ``start_phase`` / ``complete_phase`` /
    ``fail_phase`` / ``retry_phase``) carry ``destructiveHint=True`` + 10000;
    ``phase_handoff`` ALSO carries ``idempotentHint=True`` (the only tool with it).
    The bodies are thin shells over the ``handle_*`` handlers; the real DI context
    is wired by the global seam later (T6.5) — this function only makes the tools
    enumerate correctly on ``tools/list``.

    NOT called from ``tools/__init__.py`` yet: the global ``register_tools``
    handshake stays at 0 tools until the wiring task.
    """

    @mcp.tool(
        name="pipeline_get_config",
        description=(
            "Get the pipeline configuration: phases, edges, debate config, "
            "storage paths, and schema references."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_get_config() -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_init_run",
        description=(
            "Initialize a new pipeline run. Creates run state with all phases "
            "set to pending. Returns the run state and available starting phases."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_init_run(
        run_id: str,
        project_root: str,
        base_dir: str | None = None,
        skip_phases: list[str] | None = None,
        initial_artifacts: list[dict[str, str]] | None = None,
        run_parameters: dict[str, object] | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_next_phases",
        description=(
            "Resolve which phases can run next based on current run state: "
            "completed phases, available artifacts, and DAG edges."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_next_phases(
        skip_phases: list[str] | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_start_phase",
        description=(
            "Mark a phase as in-progress. Returns the phase definition (inputs, "
            "outputs, tools) so the executor knows what to do."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_start_phase(
        phase: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_complete_phase",
        description="Mark a phase as completed.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_complete_phase(
        phase: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_fail_phase",
        description="Mark a phase as failed with an error message.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_fail_phase(
        phase: str,
        error: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_retry_phase",
        description=(
            "Retry a failed phase by transitioning it back to pending so it can "
            "be started again. Only phases in the failed state can be retried."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_retry_phase(
        phase: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_run_status",
        description=(
            "Get the current run state: phase statuses, available artifacts, "
            "and overall progress."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_run_status() -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_reload_state",
        description=(
            "Reload the active run state from disk. Use this after external "
            "modifications to run-state.json."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_reload_state() -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_phase_handoff",
        description=(
            "Generate a compact handoff payload (<5K tokens) for starting a "
            "fresh agent session at a phase boundary. Contains run identity, "
            "phase statuses with artifact paths (refs only), DAG edges, next "
            "phases, quality gate summaries, and suggested instruction. Use this "
            "at phase boundaries to enable session resets that reduce quadratic "
            "context growth."
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_phase_handoff() -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_phase_brief",
        description=(
            "Generate a self-contained subagent brief for executing a given "
            "phase. Contains phase description, input artifact paths, output "
            "requirements, quality gate criteria, storage paths, tools, and "
            "structured instruction. Designed to be passed directly as a "
            "subagent prompt — no additional context needed."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_phase_brief(
        phase: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")
