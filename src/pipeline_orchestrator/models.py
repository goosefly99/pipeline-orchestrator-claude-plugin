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
