"""TOML config loaders (spec §10.1) — port of ``legacy-node/toml-loader.ts``.

Three loaders that parse the bundled ``pipeline/*.toml`` assets into the
:mod:`pipeline_orchestrator.models` dataclasses, byte-exact with the Node
``smol-toml`` originals:

* :func:`load_pipeline_config` — the full ``pipeline.toml`` (raises on a missing
  file, mirroring Node ``readFileSync``'s ``ENOENT``).
* :func:`load_quality_gates` — ``quality-gates.toml``; ``[]`` when absent.
* :func:`load_hooks_config` — lifecycle hooks; ``[]`` when absent, and filtered
  to the four valid triggers.

Parity notes carried over from the Node source:

* The ``[[edges]]`` wire key ``from`` (a Python reserved word) maps onto the
  :class:`~pipeline_orchestrator.models.EdgeDefinition` field ``from_``.
* Defaults use ``dict.get(key, default)`` (the TS ``??`` nullish coalescing),
  **never** ``or`` — an explicit falsy value (``0``, ``False``, ``""``) in the
  TOML must survive, e.g. ``max_queries_per_phase`` of ``0`` stays ``0`` and an
  explicit ``entry_point = false`` stays ``False``.
* Parsing uses stdlib :mod:`tomllib` over a binary file handle (the only
  supported mode), matching the §15 handshake-safety constraint of a pure-stdlib
  parser.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, cast

from pipeline_orchestrator.models import (
    DebateAgentConfig,
    DebateConfig,
    DebateOutputConfig,
    EdgeDefinition,
    HookConfig,
    HookTrigger,
    KBDefaults,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    QualityCheck,
    QualityGate,
    StorageConfig,
)

#: The four valid hook triggers (mirrors the TS ``validTriggers`` array). Hooks
#: with any other ``trigger`` are dropped silently (not an error).
_VALID_TRIGGERS: tuple[HookTrigger, ...] = (
    "pre_pipeline_init",
    "pre_start",
    "post_complete",
    "on_fail",
)


def _read_toml(toml_path: str | Path) -> dict[str, Any]:
    """Parse a TOML file with stdlib ``tomllib`` (binary mode, per §15).

    Raises ``FileNotFoundError`` when the path is absent, mirroring the Node
    ``readFileSync`` ``ENOENT`` (the ``loadPipelineConfig`` missing-file test
    asserts a throw rather than an empty result).
    """
    with open(toml_path, "rb") as fh:
        return tomllib.load(fh)


def load_pipeline_config(toml_path: str | Path) -> PipelineConfig:
    """Parse a full ``pipeline.toml`` into a :class:`PipelineConfig`.

    Mirrors ``loadPipelineConfig`` in ``toml-loader.ts`` table-for-table:
    ``[pipeline]`` → :class:`PipelineMeta`, ``[phases.<name>]`` →
    :class:`PhaseDefinition`, ``[[edges]]`` → :class:`EdgeDefinition` (wire key
    ``from`` → field ``from_``), ``[debate]`` (+ ``[debate.agents.*]``) →
    :class:`DebateConfig`, ``[knowledge_bases]`` → :class:`KBDefaults`,
    ``[schemas]`` → ``dict[str, str]``, ``[storage]`` (+ ``[storage.paths]``) →
    :class:`StorageConfig`. Raises ``FileNotFoundError`` on a missing file.
    """
    parsed = _read_toml(toml_path)

    raw_pipeline = cast("dict[str, Any]", parsed["pipeline"])
    raw_phases = cast("dict[str, dict[str, Any]]", parsed["phases"])
    raw_edges = cast("list[dict[str, Any]]", parsed["edges"])
    raw_debate = cast("dict[str, Any]", parsed["debate"])
    raw_kb = cast("dict[str, Any] | None", parsed.get("knowledge_bases"))
    raw_schemas = cast("dict[str, str]", parsed["schemas"])
    raw_storage = cast("dict[str, Any]", parsed["storage"])

    pipeline = PipelineMeta(
        id=raw_pipeline["id"],
        version=raw_pipeline["version"],
        description=raw_pipeline["description"],
    )

    phases: dict[str, PhaseDefinition] = {}
    for name, raw in raw_phases.items():
        phases[name] = PhaseDefinition(
            id=raw["id"],
            description=raw["description"],
            inputs=raw.get("inputs", []),
            outputs=raw.get("outputs", []),
            tools=raw.get("tools", []),
            input_mode=raw.get("input_mode"),
            output_mode=raw.get("output_mode"),
            entry_point=raw.get("entry_point", False),
            optional=raw.get("optional"),
            reusable=raw.get("reusable"),
            model_tier=raw.get("model_tier"),
            model=raw.get("model"),
        )

    edges: list[EdgeDefinition] = [
        EdgeDefinition(
            from_=e["from"],
            to=e["to"],
            note=e.get("note"),
            optional=e.get("optional"),
            input_as=e.get("input_as"),
            when_input=e.get("when_input"),
        )
        for e in raw_edges
    ]

    raw_output = raw_debate.get("output")
    output = (
        DebateOutputConfig(
            includes_transcript=raw_output["includes_transcript"],
            includes_refined_artifact=raw_output["includes_refined_artifact"],
            artifact_version_bump=raw_output["artifact_version_bump"],
        )
        if raw_output is not None
        else DebateOutputConfig(
            includes_transcript=True,
            includes_refined_artifact=True,
            artifact_version_bump="minor",
        )
    )

    agents: dict[str, DebateAgentConfig] = {}
    raw_agents = cast("dict[str, dict[str, Any]] | None", raw_debate.get("agents"))
    if raw_agents:
        for name, agent in raw_agents.items():
            agents[name] = DebateAgentConfig(
                role=agent["role"],
                runs_in=agent["runs_in"],
                depends_on=agent.get("depends_on"),
            )

    debate = DebateConfig(
        agents=agents,
        rounds=raw_debate.get("rounds", {}),
        output=output,
    )

    knowledge_bases = KBDefaults(
        available_in=(raw_kb.get("available_in", []) if raw_kb else []),
        max_queries_per_phase=(
            raw_kb.get("max_queries_per_phase", 20) if raw_kb else 20
        ),
        query_mode=(raw_kb.get("query_mode", "proactive") if raw_kb else "proactive"),
    )

    storage = StorageConfig(
        base_dir=raw_storage.get("base_dir", "strategies"),
        paths=raw_storage.get("paths", {}),
    )

    return PipelineConfig(
        pipeline=pipeline,
        phases=phases,
        edges=edges,
        debate=debate,
        knowledge_bases=knowledge_bases,
        schemas=raw_schemas,
        storage=storage,
    )


def load_quality_gates(toml_path: str | Path) -> list[QualityGate]:
    """Parse ``quality-gates.toml`` into a list of :class:`QualityGate`.

    Returns ``[]`` when the file is absent (gates are optional) or when there is
    no ``[[gates]]`` array. Mirrors ``loadQualityGates`` in ``toml-loader.ts``.
    """
    if not Path(toml_path).exists():
        return []

    parsed = _read_toml(toml_path)

    raw_gates = parsed.get("gates")
    if not raw_gates or not isinstance(raw_gates, list):
        return []

    gates: list[QualityGate] = []
    for g in cast("list[dict[str, Any]]", raw_gates):
        raw_checks = cast("list[dict[str, Any]] | None", g.get("checks"))
        checks = [
            QualityCheck(
                check_type=c["check_type"],
                description=c.get("description", ""),
                params=c.get("params", {}),
            )
            for c in (raw_checks or [])
        ]
        gates.append(
            QualityGate(
                phase=g["phase"],
                on_failure=g.get("on_failure", "warn"),
                checks=checks,
            )
        )
    return gates


def load_hooks_config(toml_path: str | Path) -> list[HookConfig]:
    """Parse lifecycle hooks from a TOML file's ``[[hooks]]`` array.

    Returns ``[]`` when the file is absent or has no ``[[hooks]]`` array. Hooks
    are filtered to the four valid triggers (others dropped, not errored).
    Mirrors ``loadHooksConfig`` in ``toml-loader.ts``.
    """
    if not Path(toml_path).exists():
        return []

    parsed = _read_toml(toml_path)

    raw_hooks = parsed.get("hooks")
    if not raw_hooks or not isinstance(raw_hooks, list):
        return []

    hooks: list[HookConfig] = []
    for h in cast("list[dict[str, Any]]", raw_hooks):
        if h.get("trigger") not in _VALID_TRIGGERS:
            continue
        hooks.append(
            HookConfig(
                trigger=cast("HookTrigger", h["trigger"]),
                phase_filter=h.get("phase_filter", "*"),
                command=h["command"],
                args=h.get("args", []),
                timeout_ms=h.get("timeout_ms", 5000),
            )
        )
    return hooks
