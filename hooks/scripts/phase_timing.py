#!/usr/bin/env python3
"""Append phase timing entries to timing.jsonl — PostToolUse hook."""
from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
LOGS_DIR = Path(
    os.environ.get("PIPELINE_HOOK_LOGS_DIR") or (_ROOT / "hooks" / "hook-logs")
)


def main() -> None:
    raw_input = read_stdin()

    try:
        hook_input: Any = json.loads(raw_input or "{}")
        tool_name = hook_input.get("tool_name") or ""
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        session_id = hook_input.get("session_id") or "unknown"

        phase_name = (
            tool_input.get("phase") or tool_input.get("phase_name") or "unknown"
        )

        # Determine event type from tool name
        event = "unknown"
        if "start_phase" in tool_name:
            event = "phase_started"
        elif "complete_phase" in tool_name:
            event = "phase_completed"

        entry = {
            "phase": phase_name,
            "event": event,
            "timestamp": datetime.now(UTC).isoformat(),
            "session_id": session_id,
        }

        LOGS_DIR.mkdir(parents=True, exist_ok=True)

        timing_path = LOGS_DIR / "timing.jsonl"
        with timing_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")

        # No output needed for PostToolUse timing — silent telemetry
        sys.exit(0)
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
