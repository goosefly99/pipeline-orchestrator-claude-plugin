#!/usr/bin/env python3
"""Control whether SendMessage is allowed/denied/asked — PreToolUse hook."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import load_config, read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(
    os.environ.get("PIPELINE_RUNTIME_CONFIG")
    or (_ROOT / "hooks" / "runtime-config.json")
)


def main() -> None:
    read_stdin()

    try:
        default: dict[str, Any] = {
            "send_message_guard": {
                "mode": "ask",
                "deny_reason": "SendMessage requires approval.",
            }
        }
        config = {**default, **load_config(CONFIG_PATH)}

        guard_config: dict[str, Any] = config.get("send_message_guard") or {}
        mode = guard_config.get("mode") or "ask"
        deny_reason = (
            guard_config.get("deny_reason")
            or "SendMessage requires approval during pipeline execution."
        )

        response: dict[str, Any]
        if mode == "allow":
            response = {"permissionDecision": "allow"}
        elif mode == "deny":
            response = {"permissionDecision": "deny", "deny_reason": deny_reason}
        else:
            response = {"permissionDecision": "ask"}

        sys.stdout.write(json.dumps(response))
    except Exception:
        # On error, default to ask
        sys.stdout.write(json.dumps({"permissionDecision": "ask"}))


if __name__ == "__main__":
    main()
