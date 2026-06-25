#!/usr/bin/env python3
"""Validate DAG dependencies before allowing phase start — PreToolUse hook."""
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


def get_phase_info(phase_name: str) -> dict[str, Any]:
    """Return ``{"isEntryPoint": bool, "requiredDeps": list}`` for a phase.

    On a missing or unparseable ``pipeline.toml`` returns
    ``{"isEntryPoint": False, "requiredDeps": []}``.
    """
    if not PIPELINE_TOML.exists():
        return {"isEntryPoint": False, "requiredDeps": []}

    try:
        with open(PIPELINE_TOML, "rb") as f:
            toml: dict[str, Any] = tomllib.load(f)
        phase_def = (toml.get("phases") or {}).get(phase_name) or {}
        is_entry_point = phase_def.get("entry_point") is True
        edges = toml.get("edges") or []
        required_deps = [
            e["from"]
            for e in edges
            if e.get("to") == phase_name and not e.get("optional")
        ]
        return {"isEntryPoint": is_entry_point, "requiredDeps": required_deps}
    except Exception:
        return {"isEntryPoint": False, "requiredDeps": []}


def main() -> None:
    raw_input = read_stdin()

    try:
        hook_input: Any = json.loads(raw_input or "{}")
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        phase_name = tool_input.get("phase") or tool_input.get("phase_name")

        if not phase_name:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        info = get_phase_info(phase_name)
        is_entry_point: bool = info["isEntryPoint"]
        required_deps: list[Any] = info["requiredDeps"]

        if is_entry_point or len(required_deps) == 0:
            # Entry point phase or no required dependencies — allow without
            # DAG check.
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        run_state = find_latest_run_state(RUNS_DIR)

        if not run_state:
            # No run state found — allow (the MCP server will handle
            # validation).
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        # DAG semantics match dag.ts:59 — a required predecessor is satisfied
        # by EITHER status 'completed' OR 'skipped'. A missing phase or any
        # other status (pending, in_progress, failed) is unmet.
        phases: dict[str, Any] = run_state["phases"]
        unmet_deps: list[Any] = []
        for dep in required_deps:
            phase = phases.get(dep)
            if not phase:
                unmet_deps.append(dep)
                continue
            if phase.get("status") not in ("completed", "skipped"):
                unmet_deps.append(dep)

        if len(unmet_deps) > 0:
            lines = []
            for dep in unmet_deps:
                phase = phases.get(dep)
                status = phase.get("status") if phase else "not found"
                lines.append(f"  {dep}: {status}")
            dep_statuses = "\n".join(lines)
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "deny",
                        "deny_reason": (
                            f'Cannot start phase "{phase_name}" — required '
                            f"dependencies not completed:\n{dep_statuses}\n\n"
                            "Complete these phases first."
                        ),
                    }
                )
            )
        else:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
    except Exception:
        sys.stdout.write(json.dumps({"permissionDecision": "allow"}))


if __name__ == "__main__":
    main()
