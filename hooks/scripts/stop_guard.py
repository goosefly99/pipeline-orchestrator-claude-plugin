#!/usr/bin/env python3
"""Warn when pipeline phases are still in-progress at stop — Stop hook."""
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

        # Prevent infinite loops: if stop_hook_active is set, allow stop.
        if hook_input.get("stop_hook_active"):
            sys.exit(0)

        run_state = find_latest_run_state(RUNS_DIR)

        if not run_state or run_state.get("status") in ("completed", "failed"):
            # No active run — allow stop.
            sys.exit(0)

        # Check for in-progress phases.
        active_phases = [
            name
            for name, p in run_state["phases"].items()
            if p.get("status") == "in_progress"
        ]

        if len(active_phases) > 0:
            phase_list = ", ".join(active_phases)
            sys.stdout.write(
                json.dumps(
                    {
                        "systemMessage": (
                            f'Warning: Pipeline run "{run_state.get("run_id")}" '
                            f"has active phases: {phase_list}. Consider "
                            "completing or failing these phases before ending "
                            "the session. Use pipeline_fail_phase to mark "
                            "incomplete work, or pipeline_complete_phase if "
                            "work is done."
                        )
                    }
                )
            )
    except Exception:
        # On error, allow stop.
        sys.exit(0)


if __name__ == "__main__":
    main()
