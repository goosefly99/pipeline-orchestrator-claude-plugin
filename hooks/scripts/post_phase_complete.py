#!/usr/bin/env python3
"""Emit next available phases after a phase completes — PostToolUse hook."""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import find_latest_run_state, read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = (
    Path(os.environ.get("PIPELINE_MCP_DATA_DIR") or (_ROOT / "pipeline_mcp_data"))
    / "runs"
)
_PIPELINE_DIR = Path(
    os.environ.get("PIPELINE_CONFIG_DIR")
    or (_ROOT / "src" / "pipeline_orchestrator" / "pipeline")
)
PIPELINE_TOML = _PIPELINE_DIR / "pipeline.toml"


def get_next_phases(run_state: Any) -> list[dict[str, Any]]:
    """Return ``[{"name": ..., "model_tier": ...}, ...]`` for available phases.

    A phase is available when it is still pending and all of its required
    (non-optional) dependencies are completed or skipped. Returns ``[]`` when
    ``pipeline.toml`` is missing/unparseable or ``run_state`` is falsy.
    """
    if not PIPELINE_TOML.exists() or not run_state:
        return []

    try:
        with open(PIPELINE_TOML, "rb") as f:
            toml: dict[str, Any] = tomllib.load(f)
        edges = toml.get("edges") or []
        phases: dict[str, Any] = toml.get("phases") or {}

        # Find phases that are still pending and whose dependencies are met.
        next_phases: list[dict[str, Any]] = []
        for phase_name, phase_def in phases.items():
            phase_state = run_state["phases"].get(phase_name)
            if phase_state and phase_state.get("status") != "pending":
                continue

            # Get required dependencies for this phase.
            required_deps = [
                e["from"]
                for e in edges
                if e.get("to") == phase_name and not e.get("optional")
            ]

            # If entry_point and no required deps, it's always available.
            if phase_def.get("entry_point") and len(required_deps) == 0:
                next_phases.append(
                    {"name": phase_name, "model_tier": phase_def.get("model_tier")}
                )
                continue

            # DAG semantics match dag.ts:59 — a required predecessor is
            # satisfied by EITHER 'completed' OR 'skipped'.
            all_met = all(
                (
                    run_state["phases"].get(dep)
                    and run_state["phases"][dep].get("status")
                    in ("completed", "skipped")
                )
                for dep in required_deps
            )

            if all_met and len(required_deps) > 0:
                next_phases.append(
                    {"name": phase_name, "model_tier": phase_def.get("model_tier")}
                )

        return next_phases
    except Exception:
        return []


def main() -> None:
    raw_input = read_stdin()

    try:
        hook_input: Any = json.loads(raw_input or "{}")
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        phase_name = (
            tool_input.get("phase") or tool_input.get("phase_name") or "unknown"
        )

        run_state = find_latest_run_state(RUNS_DIR)

        # Find next available phases.
        next_phases = get_next_phases(run_state)

        if len(next_phases) > 0:
            lines = [
                (
                    f"  {p['name']} (recommended: {p['model_tier']})"
                    if p.get("model_tier")
                    else f"  {p['name']}"
                )
                for p in next_phases
            ]
            phase_list = "\n".join(lines)
            system_message = (
                f'Phase "{phase_name}" completed. Next available phases:\n'
                f"{phase_list}\n\nConsider using /clear to reset context "
                "before starting the next phase."
            )
        else:
            system_message = (
                f'Phase "{phase_name}" completed. No more phases are available '
                "— pipeline may be complete."
            )

        sys.stdout.write(json.dumps({"systemMessage": system_message}))
    except Exception:
        # Non-blocking — exit cleanly on error.
        sys.exit(0)


if __name__ == "__main__":
    main()
