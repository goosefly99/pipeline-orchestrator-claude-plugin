"""Run-state machine (port of ``legacy-node/run-state.ts``, spec §7.3 / R2 §12).

Byte-exact behavioral port of the Node ``run-state.ts`` module: run
initialization, the five guard-then-mutate phase transitions
(``start_phase`` / ``complete_phase`` / ``fail_phase`` / ``retry_phase`` /
``skip_phase``), artifact bookkeeping, atomic persistence, integrity-checked
load, and crash recovery, plus the pure derivation helpers (recommended
action, run warnings, phase-status extractors).

Scope boundary: ``compute_run_terminal_status`` /
``finalize_run_terminal_status`` (T1.3) are ported here (DAG-aware
terminal-status detection); ``resolve_input_artifacts`` (T1.4) lives in
``dag.py`` instead. Each is exercised by its own test file.

Persistence parity notes:
  * ``persist`` writes ``JSON.stringify(state, null, 2)`` — Python
    ``json.dumps(..., indent=2)`` over a dict that OMITS ``None``-valued
    optional fields, so absent TS ``?:`` properties stay absent in the JSON
    (matching the Node object-literal serialization the tests round-trip).
  * ``atomic_rename`` writes ``{path}.tmp`` then ``os.replace``; on a Windows
    cross-volume ``PermissionError`` it falls back to ``shutil.copyfile`` +
    ``os.remove`` (the Node ``EPERM`` → ``copyFileSync`` + ``unlinkSync`` path).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from .dag import resolve_next_phases
from .errors import ErrorClass, PipelineError
from .models import ArtifactRef, PhaseState, PipelineConfig, RunState
from .storage import get_run_data_dir

STATE_FILE = "run-state.json"


# ── Time + serialization helpers (parity with Node) ─────────────────


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Mirrors Node ``new Date().toISOString()`` — a millisecond-precision,
    ``Z``-suffixed UTC timestamp (e.g. ``2026-04-10T12:34:56.789Z``).
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _phase_to_dict(phase: PhaseState) -> dict[str, Any]:
    """Serialize a :class:`PhaseState`, omitting ``None`` optional fields.

    Matches the Node object-literal JSON: ``started_at`` / ``completed_at`` /
    ``error`` are absent when unset (``undefined`` in TS is dropped by
    ``JSON.stringify``).
    """
    out: dict[str, Any] = {
        "phase_name": phase.phase_name,
        "status": phase.status,
        "input_artifacts": list(phase.input_artifacts),
        "output_artifacts": list(phase.output_artifacts),
        "retry_count": phase.retry_count,
    }
    if phase.started_at is not None:
        out["started_at"] = phase.started_at
    if phase.completed_at is not None:
        out["completed_at"] = phase.completed_at
    if phase.error is not None:
        out["error"] = phase.error
    return out


def _artifact_to_dict(ref: ArtifactRef) -> dict[str, Any]:
    """Serialize an :class:`ArtifactRef`, omitting ``None`` optional fields."""
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


def _state_to_dict(state: RunState) -> dict[str, Any]:
    """Serialize a :class:`RunState` to a JSON-able dict, omitting ``None`` fields.

    The key order mirrors the Node object literal in ``initRun`` so the written
    JSON is byte-stable across the port.
    """
    out: dict[str, Any] = {
        "run_id": state.run_id,
        "pipeline_version": state.pipeline_version,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
    }
    if state.completed_at is not None:
        out["completed_at"] = state.completed_at
    if state.run_parameters is not None:
        out["run_parameters"] = state.run_parameters
    if state.run_data_dir is not None:
        out["run_data_dir"] = state.run_data_dir
    out["status"] = state.status
    out["phases"] = {
        name: _phase_to_dict(phase) for name, phase in state.phases.items()
    }
    out["available_artifacts"] = [
        _artifact_to_dict(ref) for ref in state.available_artifacts
    ]
    out["config_path"] = state.config_path
    return out


# ── Atomic write ─────────────────────────────────────────────────────


def atomic_rename(tmp_path: str, final_path: str) -> None:
    """Atomic rename with a Windows cross-volume ``PermissionError`` fallback.

    Tries ``os.replace`` first (atomic on the same volume). On a
    ``PermissionError`` (the Node ``EPERM`` cross-volume edge case on Windows),
    falls back to ``shutil.copyfile`` + ``os.remove`` — non-atomic but handles
    cross-volume moves. Re-raises any other error.
    """
    try:
        os.replace(tmp_path, final_path)
    except PermissionError:
        shutil.copyfile(tmp_path, final_path)
        os.remove(tmp_path)


def resolve_run_write_dir(state: RunState, fallback_dir: str) -> str:
    """Resolve the effective directory for ``run-state.json`` writes.

    When ``state.run_data_dir`` is populated it takes precedence over the
    caller-supplied ``fallback_dir`` (Feature C step C8); legacy runs without a
    ``run_data_dir`` fall back to ``fallback_dir``.
    """
    return state.run_data_dir if state.run_data_dir is not None else fallback_dir


def _persist(state: RunState, state_dir: str) -> None:
    """Stamp ``updated_at``, mkdir-p the write dir, and atomically write state.

    Internal — called by every mutation. Mirrors the Node ``persist``.
    """
    write_dir = resolve_run_write_dir(state, state_dir)
    if not os.path.exists(write_dir):
        os.makedirs(write_dir, exist_ok=True)
    state.updated_at = _now_iso()
    final_path = os.path.join(write_dir, STATE_FILE)
    tmp_path = final_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(_state_to_dict(state), indent=2))
    atomic_rename(tmp_path, final_path)


# ── Run initialization ───────────────────────────────────────────────


def init_run(
    run_id: str,
    pipeline_version: str,
    phase_names: list[str],
    state_dir: str,
    run_parameters: dict[str, Any] | None = None,
) -> RunState:
    """Create, persist, and return a new :class:`RunState` with all phases pending.

    Computes ``run_data_dir`` (Feature C) only when ``run_parameters`` carries
    BOTH ``run_name`` and ``run_directory_timestamp``; otherwise warns that
    parameterization is inactive and leaves ``run_data_dir`` unset.
    """
    phases: dict[str, PhaseState] = {}
    for name in phase_names:
        phases[name] = PhaseState(
            phase_name=name,
            status="pending",
            input_artifacts=[],
            output_artifacts=[],
            retry_count=0,
        )

    state = RunState(
        run_id=run_id,
        pipeline_version=pipeline_version,
        created_at=_now_iso(),
        updated_at=_now_iso(),
        status="initialized",
        phases=phases,
        available_artifacts=[],
        config_path=state_dir,
    )

    # Persist run_parameters only when the caller provided a non-empty object.
    # Empty {} is omitted so legacy runs and runs without parameters stay clean.
    if run_parameters and len(run_parameters) > 0:
        state.run_parameters = run_parameters

    # Feature C: compute per-run data directory from run_name + timestamp.
    # Requires the caller invariant `state_dir == {base_dir}/runs/{run_id}` so
    # we can recover base_dir via two dirname() calls.
    run_name = run_parameters.get("run_name") if run_parameters else None
    run_ts = (
        run_parameters.get("run_directory_timestamp") if run_parameters else None
    )
    if run_name and run_ts:
        base_dir = os.path.dirname(os.path.dirname(state_dir))
        state.run_data_dir = get_run_data_dir(base_dir, str(run_name), str(run_ts))
    else:
        print(
            f"[pipeline] initRun({run_id}): run_data_dir parameterization "
            "inactive (run_name and/or run_directory_timestamp absent). "
            f"Falling back to legacy {state_dir}.",
            file=sys.stderr,
        )

    _persist(state, state_dir)
    return state


# ── Guard-then-mutate phase transitions ──────────────────────────────


def _require_phase(state: RunState, phase_name: str) -> PhaseState:
    """Return the named phase or raise the byte-exact "not found" error."""
    phase = state.phases.get(phase_name)
    if phase is None:
        raise PipelineError(
            f'Phase "{phase_name}" not found in run state',
            ErrorClass.state_transition_error,
            recovery_action="Verify phase name exists in pipeline configuration.",
            details={"phase": phase_name},
        )
    return phase


def start_phase(state: RunState, phase_name: str, state_dir: str) -> RunState:
    """Transition a ``pending`` phase to ``in_progress`` and set the run running."""
    phase = _require_phase(state, phase_name)
    if phase.status != "pending":
        recovery = (
            "Call pipeline_retry_phase to reset to pending, then start."
            if phase.status == "failed"
            else f'Phase is "{phase.status}". Only pending phases can be started.'
        )
        raise PipelineError(
            f'Cannot start phase "{phase_name}": status is "{phase.status}" '
            '(expected "pending")',
            ErrorClass.state_transition_error,
            recovery_action=recovery,
            details={
                "phase": phase_name,
                "current_status": phase.status,
                "expected_status": "pending",
            },
        )

    phase.status = "in_progress"
    phase.started_at = _now_iso()
    state.status = "running"

    _persist(state, state_dir)
    return state


def complete_phase(state: RunState, phase_name: str, state_dir: str) -> RunState:
    """Transition an ``in_progress`` phase to ``completed``; finalize run if done."""
    phase = _require_phase(state, phase_name)
    if phase.status != "in_progress":
        recovery = (
            "Call pipeline_start_phase first."
            if phase.status == "pending"
            else f'Phase is "{phase.status}". Only in_progress phases can be completed.'
        )
        raise PipelineError(
            f'Cannot complete phase "{phase_name}": status is "{phase.status}" '
            '(expected "in_progress")',
            ErrorClass.state_transition_error,
            recovery_action=recovery,
            details={
                "phase": phase_name,
                "current_status": phase.status,
                "expected_status": "in_progress",
            },
        )

    phase.status = "completed"
    phase.completed_at = _now_iso()

    all_done = all(
        p.status == "completed" or p.status == "skipped"
        for p in state.phases.values()
    )
    if all_done:
        state.status = "completed"
        state.completed_at = _now_iso()

    _persist(state, state_dir)
    return state


def fail_phase(
    state: RunState, phase_name: str, error: str, state_dir: str
) -> RunState:
    """Transition an ``in_progress`` phase to ``failed`` and mark the run failed."""
    phase = _require_phase(state, phase_name)
    if phase.status != "in_progress":
        recovery = (
            "Call pipeline_start_phase first before marking as failed."
            if phase.status == "pending"
            else f'Phase is "{phase.status}". Only in_progress phases can be failed.'
        )
        raise PipelineError(
            f'Cannot fail phase "{phase_name}": status is "{phase.status}" '
            '(expected "in_progress")',
            ErrorClass.state_transition_error,
            recovery_action=recovery,
            details={
                "phase": phase_name,
                "current_status": phase.status,
                "expected_status": "in_progress",
            },
        )

    phase.status = "failed"
    phase.error = error
    state.status = "failed"
    state.completed_at = _now_iso()

    _persist(state, state_dir)
    return state


def retry_phase(state: RunState, phase_name: str, state_dir: str) -> RunState:
    """Reset a ``failed`` phase to ``pending`` and increment ``retry_count``."""
    phase = _require_phase(state, phase_name)
    if phase.status != "failed":
        recovery = (
            "Call pipeline_fail_phase first, then retry."
            if phase.status == "in_progress"
            else f'Phase is "{phase.status}". Only failed phases can be retried.'
        )
        raise PipelineError(
            f'Cannot retry phase "{phase_name}": status is "{phase.status}" '
            '(expected "failed")',
            ErrorClass.state_transition_error,
            recovery_action=recovery,
            details={
                "phase": phase_name,
                "current_status": phase.status,
                "expected_status": "failed",
            },
        )

    phase.status = "pending"
    phase.error = None
    phase.retry_count += 1

    _persist(state, state_dir)
    return state


def skip_phase(state: RunState, phase_name: str, state_dir: str) -> RunState:
    """Transition a ``pending`` phase to ``skipped``."""
    phase = _require_phase(state, phase_name)
    if phase.status != "pending":
        recovery = (
            "Call pipeline_fail_phase or pipeline_complete_phase first."
            if phase.status == "in_progress"
            else f'Phase is "{phase.status}". Only pending phases can be skipped.'
        )
        raise PipelineError(
            f'Cannot skip phase "{phase_name}": status is "{phase.status}" '
            '(expected "pending")',
            ErrorClass.state_transition_error,
            recovery_action=recovery,
            details={
                "phase": phase_name,
                "current_status": phase.status,
                "expected_status": "pending",
            },
        )

    phase.status = "skipped"
    _persist(state, state_dir)
    return state


# ── Artifact bookkeeping ─────────────────────────────────────────────


def add_artifact(state: RunState, ref: ArtifactRef, state_dir: str) -> RunState:
    """Append an artifact ref and record its path on the producing phase."""
    state.available_artifacts.append(ref)

    phase = state.phases.get(ref.phase)
    if phase is not None:
        phase.output_artifacts.append(ref.path)

    _persist(state, state_dir)
    return state


def remove_phase_artifacts(
    state: RunState, phase_name: str, state_dir: str
) -> list[str]:
    """Remove all artifact refs for ``phase_name`` from run state.

    Used during quality-gate rollback to prevent orphaned artifacts from
    satisfying downstream DAG inputs. Returns the removed artifact paths.
    """
    removed: list[str] = []
    kept: list[ArtifactRef] = []
    for ref in state.available_artifacts:
        if ref.phase == phase_name:
            removed.append(ref.path)
        else:
            kept.append(ref)
    state.available_artifacts = kept

    phase = state.phases.get(phase_name)
    if phase is not None:
        phase.output_artifacts = []

    _persist(state, state_dir)
    return removed


# ── Integrity validation + load ──────────────────────────────────────


def validate_run_state_integrity(state: object) -> str | None:
    """Validate structural integrity of a parsed run-state object.

    Returns an error message string if invalid, or ``None`` if valid. Operates
    on the raw parsed JSON (a ``dict``), mirroring the Node ``unknown`` input.
    """
    if state is None or not isinstance(state, dict):
        return "Run state is not a valid object"

    obj: dict[str, Any] = state

    # updated_at must be a valid ISO date string
    updated_at = obj.get("updated_at")
    if not isinstance(updated_at, str):
        return "Run state missing updated_at field"
    if not _is_valid_iso_date(updated_at):
        return f'Run state updated_at is not a valid ISO date: "{updated_at}"'

    # phases must be a non-empty object
    phases = obj.get("phases")
    if phases is None or not isinstance(phases, dict):
        return "Run state phases is not a valid object"
    if len(phases) == 0:
        return "Run state phases is empty — at least one phase is required"

    # run_id must be present
    run_id = obj.get("run_id")
    if not isinstance(run_id, str) or len(run_id) == 0:
        return "Run state missing or empty run_id"

    # status must be a valid run status
    valid_statuses = ["initialized", "running", "completed", "failed"]
    status = obj.get("status")
    if not isinstance(status, str) or status not in valid_statuses:
        return f'Run state has invalid status: "{status}"'

    return None


def _is_valid_iso_date(value: str) -> bool:
    """Return whether ``value`` parses as a date, mirroring JS ``new Date(...)``.

    JS ``new Date(str)`` is lenient: it accepts full ISO 8601 strings (the
    common case here) and reports ``NaN`` only for unparseable input. We accept
    the ISO forms the pipeline writes (``...Z`` and offset/naive variants) and
    reject everything else.
    """
    candidate = value
    # Python's fromisoformat (3.11+) handles offsets and 'Z'-less strings;
    # normalize a trailing 'Z' to '+00:00' for pre-3.11-style safety.
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True


def _coerce_phase(name: str, raw: dict[str, Any]) -> PhaseState:
    """Build a :class:`PhaseState` from a parsed JSON object, normalizing fields."""
    retry_count = raw.get("retry_count")
    if retry_count is None:
        retry_count = 0
    return PhaseState(
        phase_name=raw.get("phase_name", name),
        status=raw["status"],
        started_at=raw.get("started_at"),
        completed_at=raw.get("completed_at"),
        input_artifacts=list(raw.get("input_artifacts", [])),
        output_artifacts=list(raw.get("output_artifacts", [])),
        error=raw.get("error"),
        retry_count=retry_count,
    )


def _coerce_artifact(raw: dict[str, Any]) -> ArtifactRef:
    """Build an :class:`ArtifactRef` from a parsed JSON object, normalizing version."""
    version = raw.get("version")
    if version is None:
        version = 1
    return ArtifactRef(
        type=raw["type"],
        path=raw["path"],
        phase=raw["phase"],
        created_at=raw["created_at"],
        version=version,
        parent_artifact=raw.get("parent_artifact"),
    )


def load_run_state(state_dir: str) -> RunState | None:
    """Load + validate the persisted ``run-state.json`` into a :class:`RunState`.

    Returns ``None`` when the file is absent or unreadable. Raises
    ``PipelineError(file_io_error, ...)`` on invalid JSON or integrity failure.
    Applies backward-compat normalizations: missing ``retry_count`` → ``0`` and
    missing artifact ``version`` → ``1``.
    """
    file_path = os.path.join(state_dir, STATE_FILE)
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError:
        return None

    try:
        parsed: Any = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise PipelineError(
            f"Corrupt run-state.json: invalid JSON in {file_path}",
            ErrorClass.file_io_error,
            recovery_action="Delete the corrupt file and re-initialize the run.",
            details={"path": file_path},
        ) from exc

    integrity_error = validate_run_state_integrity(parsed)
    if integrity_error:
        raise PipelineError(
            f"Corrupt run-state.json: {integrity_error}",
            ErrorClass.file_io_error,
            recovery_action="Delete the corrupt file and re-initialize the run.",
            details={"path": file_path},
        )

    obj: dict[str, Any] = parsed

    phases = {
        name: _coerce_phase(name, raw_phase)
        for name, raw_phase in obj["phases"].items()
    }
    artifacts = [
        _coerce_artifact(raw_artifact)
        for raw_artifact in obj.get("available_artifacts", [])
    ]

    return RunState(
        run_id=obj["run_id"],
        pipeline_version=obj.get("pipeline_version", ""),
        created_at=obj.get("created_at", ""),
        updated_at=obj["updated_at"],
        status=obj["status"],
        config_path=obj.get("config_path", ""),
        phases=phases,
        available_artifacts=artifacts,
        completed_at=obj.get("completed_at"),
        run_parameters=obj.get("run_parameters"),
        run_data_dir=obj.get("run_data_dir"),
    )


# ── Recovery ─────────────────────────────────────────────────────────


@dataclass
class RecoveryResult:
    """Result of :func:`recover_run` (mirrors the TS ``RecoveryResult``)."""

    state: RunState
    recovered_phases: list[str]
    warnings: list[str]


def recover_run(run_dir: str) -> RecoveryResult:
    """Recover a run from its persisted state on disk.

    Loads + validates ``run-state.json``, resets any ``in_progress`` phases
    (crash artifacts) to ``pending`` with ``retry_count += 1`` and a cleared
    ``started_at``, repairs the run-level status, persists, and returns the
    recovered state plus the reset phase names and warnings.
    """
    state = load_run_state(run_dir)
    if state is None:
        raise PipelineError(
            f"No run state found in {run_dir}",
            ErrorClass.file_io_error,
            recovery_action="Initialize a new run with pipeline_init_run.",
            details={"path": run_dir},
        )

    recovered_phases: list[str] = []
    warnings: list[str] = []

    # Reset any in_progress phases back to pending (crash recovery)
    for name, phase in state.phases.items():
        if phase.status == "in_progress":
            phase.status = "pending"
            phase.retry_count += 1
            phase.started_at = None
            recovered_phases.append(name)
            warnings.append(
                f'Phase "{name}" was in_progress at shutdown — reset to pending '
                f"(retry #{phase.retry_count})"
            )

    # If any phases were recovered and the run was 'running', check if it should
    # revert to 'initialized'.
    if len(recovered_phases) > 0 and state.status == "running":
        has_in_progress = any(
            p.status == "in_progress" for p in state.phases.values()
        )
        if not has_in_progress:
            has_completed = any(
                p.status == "completed" for p in state.phases.values()
            )
            state.status = "running" if has_completed else "initialized"

    # If the run had failed status but no phases are actually failed, fix it.
    if state.status == "failed":
        has_failed = any(p.status == "failed" for p in state.phases.values())
        if not has_failed:
            has_completed = any(
                p.status == "completed" for p in state.phases.values()
            )
            state.status = "running" if has_completed else "initialized"
            warnings.append(
                'Run status was "failed" but no phases are failed — status corrected'
            )

    _persist(state, run_dir)

    return RecoveryResult(
        state=state, recovered_phases=recovered_phases, warnings=warnings
    )


# ── Phase-status extraction helpers (pure) ──────────────────────────


def get_completed_phases(state: RunState) -> list[str]:
    """Return names of all phases with ``completed`` status."""
    return [
        name for name, p in state.phases.items() if p.status == "completed"
    ]


def get_skipped_phases(state: RunState) -> list[str]:
    """Return names of all phases with ``skipped`` status."""
    return [name for name, p in state.phases.items() if p.status == "skipped"]


def get_in_progress_phases(state: RunState) -> list[str]:
    """Return names of all phases with ``in_progress`` status."""
    return [
        name for name, p in state.phases.items() if p.status == "in_progress"
    ]


def get_artifact_types(state: RunState) -> list[str]:
    """Return all artifact types from ``available_artifacts`` (preserving order)."""
    return [a.type for a in state.available_artifacts]


# ── Run terminal-status detection (DAG-aware) ───────────────────────


def compute_run_terminal_status(
    state: RunState, config: PipelineConfig
) -> Literal["running", "completed", "failed"]:
    """Determine whether the run as a whole has reached a terminal state.

    Ported from ``computeRunTerminalStatus`` in ``run-state.ts``. Returns one of
    ``'running' | 'completed' | 'failed'`` by this precedence (return at the
    first match):

    1. Any phase ``'failed'``      → ``'failed'``.
    2. Any phase ``'in_progress'`` → ``'running'``.
    2.5. Any terminal phase (no outgoing edges) ``'completed'`` → ``'completed'``
         (pending feedback-loop phases are treated as effectively done), guarded
         by every ``'pending'`` phase having at least one incoming edge.
    3. ``resolve_next_phases`` empty → ``'completed'`` (no more forward progress
       possible; pending phases from untaken DAG branches are treated as
       effectively done because the DAG will never make them runnable).
    4. otherwise                   → ``'running'``.

    Note: the plan document labels the terminal success state ``'done'``, but the
    concrete ``RunState.status`` union uses ``'completed'`` — this helper returns
    ``'completed'`` to stay consistent with the type and
    ``validate_run_state_integrity``.
    """
    phases = list(state.phases.values())

    if any(p.status == "failed" for p in phases):
        return "failed"
    if any(p.status == "in_progress" for p in phases):
        return "running"

    # Terminal-node detection: if any phase with no outgoing DAG edges ('terminal
    # node') has completed, the run's primary path is exhausted. Pending phases
    # that remain reachable only via feedback loops (e.g. research_discovery via
    # the validation -> research_discovery edge) are treated as effectively done.
    # Guard: only apply this if every pending phase has at least one incoming edge
    # (i.e. no pending phase is an independent entry-point that can run on its own).
    terminal_node_completed = any(
        phase_state.status == "completed"
        and all(e.from_ != name for e in config.edges)
        for name, phase_state in state.phases.items()
    )
    pending_phases = [
        name
        for name, phase_state in state.phases.items()
        if phase_state.status == "pending"
    ]
    all_pending_have_incoming_edges = all(
        any(e.to == name for e in config.edges) for name in pending_phases
    )
    if terminal_node_completed and all_pending_have_incoming_edges:
        return "completed"

    completed = get_completed_phases(state)
    skipped = get_skipped_phases(state)
    artifact_types = get_artifact_types(state)
    next_phases = resolve_next_phases(config, completed, artifact_types, skipped)

    return "completed" if len(next_phases) == 0 else "running"


def finalize_run_terminal_status(
    state: RunState,
    new_status: Literal["completed", "failed"],
    state_dir: str,
) -> RunState:
    """Set the run's terminal status fields and persist atomically.

    Ported from ``finalizeRunTerminalStatus`` in ``run-state.ts``. Used by
    lifecycle handlers after :func:`compute_run_terminal_status` reports a
    terminal result (``new_status`` is ``'completed'`` or ``'failed'``). Mutates
    ``status`` + ``completed_at`` and persists via the atomic write path.
    """
    state.status = new_status
    state.completed_at = _now_iso()
    _persist(state, state_dir)
    return state


# ── Run state derivations (pure, for recommendations and warnings) ──


@dataclass
class RecommendedAction:
    """Recommended next action for a run (mirrors the TS ``RecommendedAction``).

    ``phase`` is the ``?:`` optional and defaults to ``None`` so it is absent
    when the recommendation is not phase-scoped.
    """

    action: str
    reason: str
    phase: str | None = None


STALE_PHASE_THRESHOLD_MS = 3_600_000  # 1 hour


def compute_recommended_action(
    state: RunState, next_phases: list[str]
) -> RecommendedAction:
    """Derive the recommended next action from run state + available next phases.

    Precedence: an ``in_progress`` phase → ``continue``; else a ``failed`` phase
    → ``retry``; else the first ``next_phases`` entry → ``start_phase``; else
    ``validate`` when the run is ``completed``; else ``review``.
    """
    in_progress = next(
        (
            (name, p)
            for name, p in state.phases.items()
            if p.status == "in_progress"
        ),
        None,
    )
    if in_progress is not None:
        name = in_progress[0]
        return RecommendedAction(
            action="continue", phase=name, reason=f'Phase "{name}" is in progress.'
        )
    failed = next(
        (
            (name, p)
            for name, p in state.phases.items()
            if p.status == "failed"
        ),
        None,
    )
    if failed is not None:
        name = failed[0]
        return RecommendedAction(
            action="retry",
            phase=name,
            reason=f'Phase "{name}" failed. Call pipeline_retry_phase to retry.',
        )
    if len(next_phases) > 0:
        return RecommendedAction(
            action="start_phase",
            phase=next_phases[0],
            reason=f"Next available phase: {next_phases[0]}.",
        )
    if state.status == "completed":
        return RecommendedAction(
            action="validate",
            reason="All phases complete. Call pipeline_validate_run.",
        )
    return RecommendedAction(
        action="review",
        reason="No phases available. Check for blocked or failed phases.",
    )


def compute_run_warnings(
    state: RunState,
    optional_phases: set[str],
    now_ms: float | None = None,
) -> list[str]:
    """Derive run-level warnings: stale in-progress phases + failed optional phases.

    ``now_ms`` defaults to the current epoch milliseconds (mirrors the Node
    ``Date.now()`` default) but can be injected for deterministic stale checks.
    """
    if now_ms is None:
        now_ms = datetime.now(UTC).timestamp() * 1000

    warnings: list[str] = []
    stale_threshold = now_ms - STALE_PHASE_THRESHOLD_MS

    # Detect stale in-progress phases
    for name, p in state.phases.items():
        if (
            p.status == "in_progress"
            and p.started_at
            and _iso_to_ms(p.started_at) < stale_threshold
        ):
            warnings.append(
                f'Phase "{name}" has been in_progress for over 1 hour '
                f"(started {p.started_at})."
            )

    # Detect failed optional phases
    for name, p in state.phases.items():
        if p.status == "failed" and name in optional_phases:
            error_text = p.error if p.error is not None else "unknown"
            warnings.append(
                f'Optional phase "{name}" failed: {error_text}. Consider skipping.'
            )

    return warnings


def _iso_to_ms(iso: str) -> float:
    """Convert an ISO 8601 timestamp to epoch milliseconds (parity with JS getTime)."""
    candidate = iso
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    return datetime.fromisoformat(candidate).timestamp() * 1000
