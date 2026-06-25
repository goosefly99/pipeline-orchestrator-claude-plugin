#!/usr/bin/env python3
"""Scan for active run state and inject context — SessionStart hook."""
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
    # Read hook input from stdin (drain it even though session-init doesn't
    # consume the body).
    read_stdin()

    try:
        latest_run = find_latest_run_state(RUNS_DIR)

        if not latest_run:
            sys.exit(0)

        # Build context summary. The "next available phases" line was removed
        # in Fix 6.6 because the prior implementation listed ALL pending phases
        # without DAG dependency checks — producing misleading "next up"
        # suggestions. Users should call pipeline_next_phases for an accurate,
        # DAG-gated list.
        phases: dict[str, Any] = latest_run["phases"]
        phase_statuses = "\n".join(
            f"  {name}: {p['status']}" for name, p in phases.items()
        )

        message = "\n".join(
            [
                f"Active pipeline run: {latest_run['run_id']}",
                f"Status: {latest_run['status']}",
                f"Phases:\n{phase_statuses}",
                "",
                "Call pipeline_next_phases to see which phases are actually "
                "available (DAG-gated).",
                "Call pipeline_run_status to get full state before making "
                "changes.",
                "Consider using /clear between phases to reset context.",
            ]
        )

        response = {"systemMessage": message}
        sys.stdout.write(json.dumps(response))
    except Exception:
        # Non-blocking — exit cleanly on error
        sys.exit(0)


if __name__ == "__main__":
    main()
