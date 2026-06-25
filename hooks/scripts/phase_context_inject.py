#!/usr/bin/env python3
"""Inject phase context and quality-gate criteria on phase start — PostToolUse hook."""
from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_PIPELINE_DIR = Path(
    os.environ.get("PIPELINE_CONFIG_DIR")
    or (_ROOT / "src" / "pipeline_orchestrator" / "pipeline")
)
PIPELINE_TOML = _PIPELINE_DIR / "pipeline.toml"
QUALITY_GATES_TOML = _PIPELINE_DIR / "quality-gates.toml"


def load_phase_definition(phase_name: str) -> dict[str, Any] | None:
    """Return the ``[phases.<name>]`` table, or ``None`` if absent/unreadable."""
    if not PIPELINE_TOML.exists():
        return None

    try:
        with open(PIPELINE_TOML, "rb") as f:
            toml: dict[str, Any] = tomllib.load(f)
        phase_def: dict[str, Any] | None = (toml.get("phases") or {}).get(phase_name)
        return phase_def or None
    except Exception:
        return None


def load_quality_gate(phase_name: str) -> dict[str, Any] | None:
    """Return the first ``[[gates]]`` entry for the phase, or ``None``."""
    if not QUALITY_GATES_TOML.exists():
        return None

    try:
        with open(QUALITY_GATES_TOML, "rb") as f:
            toml: dict[str, Any] = tomllib.load(f)
        gates = toml.get("gates") or []
        for gate in gates:
            if gate.get("phase") == phase_name:
                result: dict[str, Any] = gate
                return result
        return None
    except Exception:
        return None


def main() -> None:
    raw_input = read_stdin()

    try:
        hook_input: Any = json.loads(raw_input or "{}")
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        phase_name = tool_input.get("phase") or tool_input.get("phase_name")

        if not phase_name:
            sys.exit(0)

        phase_def = load_phase_definition(phase_name)

        if not phase_def:
            sys.exit(0)

        lines = [
            f"Phase: {phase_name}",
            f"Description: {phase_def.get('description') or 'No description'}",
        ]

        outputs = phase_def.get("outputs")
        if outputs:
            lines.append("Expected outputs: " + ", ".join(outputs))

        tools = phase_def.get("tools")
        if tools:
            lines.append("Available tools: " + ", ".join(tools))

        if phase_def.get("model_tier"):
            lines.append(f"Recommended model: {phase_def['model_tier']}")

        # Add quality gate criteria.
        gate = load_quality_gate(phase_name)
        if gate:
            lines.append("")
            lines.append(
                f"Quality gate (on_failure: {gate.get('on_failure') or 'warn'}):"
            )
            for check in gate.get("checks") or []:
                description = check.get("description") or json.dumps(
                    check.get("params") or {}
                )
                lines.append(f"  - {check.get('check_type')}: {description}")

        lines.append("")
        lines.append(
            "Store artifacts with pipeline_store_artifact before calling "
            "pipeline_complete_phase."
        )

        sys.stdout.write(json.dumps({"systemMessage": "\n".join(lines)}))
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
