#!/usr/bin/env python3
"""Verify a phase has artifacts before allowing completion — PreToolUse hook."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import find_latest_run_state, read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = (
    Path(os.environ.get("PIPELINE_MCP_DATA_DIR") or (_ROOT / "pipeline_mcp_data"))
    / "runs"
)


def main() -> None:
    raw_input = read_stdin()

    try:
        hook_input: Any = json.loads(raw_input or "{}")
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        phase_name = tool_input.get("phase") or tool_input.get("phase_name")

        if not phase_name:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        run_state = find_latest_run_state(RUNS_DIR)

        if not run_state:
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)

        # Check available_artifacts for this phase
        phase_artifacts = [
            a
            for a in (run_state.get("available_artifacts") or [])
            if a.get("phase") == phase_name
        ]

        # Also check phase output_artifacts
        phases: dict[str, Any] = run_state["phases"]
        phase_state: dict[str, Any] | None = phases.get(phase_name)
        output_artifacts = (
            (phase_state.get("output_artifacts") or []) if phase_state else []
        )

        if len(phase_artifacts) == 0 and len(output_artifacts) == 0:
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "deny",
                        "deny_reason": (
                            f'Cannot complete phase "{phase_name}" — no '
                            f"artifacts have been stored. Use "
                            f"pipeline_store_artifact to save at least one "
                            f"output before completing."
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
