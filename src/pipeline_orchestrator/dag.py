"""DAG resolution (port of ``legacy-node/dag.ts``, spec §7.2).

Byte-exact behavioral port of the Node ``dag.ts`` module: the pure, config-only
DAG helpers that decide which phases are runnable next and how the typed edges
connect phases. Plus :func:`resolve_input_artifacts`, which lives in
``run-state.ts`` in the Node tree (around line 624) but the Python layout places
it here (roadmap T1.4) — it was deliberately NOT ported into ``run_state.py`` (T1.2).

Function set (snake_case, mirroring the Node call convention the tests use):
  * :func:`get_phase_input_satisfaction` — ``can_run`` decided by ``input_mode``.
  * :func:`resolve_next_phases` — entry-point bypass + required-edge predecessor
    satisfaction (``skipped`` counts as ``completed``) + optional edges never block.
  * :func:`get_downstream_phases` / :func:`get_upstream_phases` — edge-walk helpers.
  * :func:`resolve_input_artifacts` — collect upstream output paths along incoming
    edges (the ``'pre-existing'`` pseudo-source is NEVER a DAG node).

These are pure functions over :class:`PipelineConfig` / :class:`PhaseDefinition`
/ :class:`EdgeDefinition` (defined in ``models.py`` for T1.5 reuse); only
:func:`resolve_input_artifacts` touches :class:`RunState`.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PhaseDefinition, PipelineConfig, RunState


@dataclass
class InputSatisfaction:
    """Result of input resolution (mirrors TS ``InputSatisfaction``).

    ``satisfied`` / ``missing`` partition ``phase.inputs`` against the available
    artifacts (preserving phase-input order); ``can_run`` is the input-mode
    decision (see :func:`get_phase_input_satisfaction`).
    """

    satisfied: list[str]
    missing: list[str]
    can_run: bool


def get_phase_input_satisfaction(
    phase: PhaseDefinition,
    available_artifacts: list[str],
) -> InputSatisfaction:
    """Partition a phase's inputs and decide ``can_run`` from its ``input_mode``.

    Mirrors ``getPhaseInputSatisfaction`` in ``dag.ts``. ``can_run`` rules:
      * ``any`` / ``one_of`` / ``one_of_primary`` — at least one input available.
      * ``required_optional`` — the FIRST input (the required one) is available.
      * default (``None`` / unset) — the phase has no inputs, or none are missing.
    """
    satisfied = [i for i in phase.inputs if i in available_artifacts]
    missing = [i for i in phase.inputs if i not in available_artifacts]

    can_run: bool
    if phase.input_mode in ("any", "one_of", "one_of_primary"):
        can_run = len(satisfied) > 0
    elif phase.input_mode == "required_optional":
        can_run = len(phase.inputs) > 0 and phase.inputs[0] in available_artifacts
    else:
        can_run = len(phase.inputs) == 0 or len(missing) == 0

    return InputSatisfaction(satisfied=satisfied, missing=missing, can_run=can_run)


def resolve_next_phases(
    config: PipelineConfig,
    completed_phases: list[str],
    available_artifacts: list[str],
    skip_phases: list[str] | None = None,
) -> list[str]:
    """Resolve the set of phases that are runnable next.

    Mirrors ``resolveNextPhases`` in ``dag.ts``. A phase is a candidate when:
      * it is neither completed nor skipped, AND
      * it is an entry point (which bypasses input satisfaction), or its inputs
        are satisfied per :func:`get_phase_input_satisfaction`, AND
      * if it has incoming edges: either it has no required (non-optional) edges,
        or at least one required edge's predecessor is completed/skipped — where
        ``skipped`` counts as ``completed`` (a skipped predecessor satisfies the
        edge), and entry points bypass this check too (optional edges never block).
    """
    if skip_phases is None:
        skip_phases = []
    completed = set(completed_phases)
    skipped = set(skip_phases)
    candidates: list[str] = []

    for name, phase in config.phases.items():
        if name in completed or name in skipped:
            continue

        # Entry-point phases accept external input — bypass input satisfaction check
        if not phase.entry_point:
            if not get_phase_input_satisfaction(phase, available_artifacts).can_run:
                continue

        incoming_edges = [e for e in config.edges if e.to == name]
        if len(incoming_edges) > 0:
            required_edges = [e for e in incoming_edges if not e.optional]
            has_completed_or_skipped_predecessor = len(required_edges) == 0 or any(
                e.from_ in completed or e.from_ in skipped for e in required_edges
            )

            if not has_completed_or_skipped_predecessor and not phase.entry_point:
                continue

        candidates.append(name)

    return candidates


def get_downstream_phases(
    config: PipelineConfig,
    phase_name: str,
) -> list[str]:
    """Return the names of phases reachable by an outgoing edge from ``phase_name``.

    Mirrors ``getDownstreamPhases`` in ``dag.ts``: every ``edge.to`` for edges
    whose ``edge.from == phase_name`` (edge order preserved, no dedup).
    """
    return [e.to for e in config.edges if e.from_ == phase_name]


def get_upstream_phases(
    config: PipelineConfig,
    phase_name: str,
) -> list[str]:
    """Return the names of phases that have an outgoing edge into ``phase_name``.

    Mirrors ``getUpstreamPhases`` in ``dag.ts``: every ``edge.from`` for edges
    whose ``edge.to == phase_name`` (edge order preserved, no dedup).
    """
    return [e.from_ for e in config.edges if e.to == phase_name]


def resolve_input_artifacts(
    state: RunState,
    phase_name: str,
    config: PipelineConfig,
) -> list[str]:
    """Resolve artifact paths ``phase_name`` can consume from its incoming edges.

    Ported from ``resolveInputArtifacts`` in ``run-state.ts`` (~line 624). Walks
    ``config.edges`` for edges whose ``to == phase_name``, then collects every
    ``output_artifacts`` path from the upstream phase ``state.phases[edge.from]``.
    The result is deduplicated while preserving first-seen order.

    Notes:
      * Upstream phases whose status is ``pending`` are not skipped — if they
        happen to have ``output_artifacts`` recorded, those paths are included.
        In practice only completed/skipped upstream phases contribute.
      * Artifacts registered with phase ``'pre-existing'`` are NOT included:
        ``'pre-existing'`` is a pseudo-source, never a real DAG node, so it never
        appears as an ``edge.from`` and ``state.phases.get(...)`` misses it.
      * Returns an empty list for entry-point phases that have no incoming edges.
    """
    resolved: list[str] = []
    seen: set[str] = set()

    for edge in config.edges:
        if edge.to != phase_name:
            continue
        upstream = state.phases.get(edge.from_)
        if upstream is None:
            continue
        for path in upstream.output_artifacts:
            if path in seen:
                continue
            seen.add(path)
            resolved.append(path)

    return resolved
