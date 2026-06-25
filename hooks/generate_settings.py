#!/usr/bin/env python3
"""Read hooks-config.toml, write runtime-config.json, and merge hook entries
into ``.claude/settings.local.json``.
"""

from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any

_HOOKS_DIR = Path(__file__).resolve().parent
_ROOT = _HOOKS_DIR.parent

# Hook definitions: (configKey, event, matcher_or_None, scriptFile).
_HOOK_DEFS: list[tuple[str, str, str | None, str]] = [
    ("session_start_clear", "SessionStart", None, "session_init.py"),
    ("send_message_guard", "PreToolUse", "SendMessage", "send_message_guard.py"),
    (
        "token_budget_guard",
        "PreToolUse",
        "mcp__pipeline__pipeline_.*",
        "token_budget_guard.py",
    ),
    (
        "phase_session_isolation",
        "PreToolUse",
        "mcp__pipeline__pipeline_start_phase",
        "phase_start_guard.py",
    ),
    (
        "dag_guard",
        "PreToolUse",
        "mcp__pipeline__pipeline_start_phase",
        "dag_guard.py",
    ),
    (
        "artifact_gate",
        "PreToolUse",
        "mcp__pipeline__pipeline_complete_phase",
        "artifact_gate.py",
    ),
    ("file_read_version_guard", "PreToolUse", "Read", "file_read_version_guard.py"),
    (
        "post_phase_complete",
        "PostToolUse",
        "mcp__pipeline__pipeline_complete_phase",
        "post_phase_complete.py",
    ),
    (
        "phase_context_inject",
        "PostToolUse",
        "mcp__pipeline__pipeline_start_phase",
        "phase_context_inject.py",
    ),
    (
        "phase_timing",
        "PostToolUse",
        "mcp__pipeline__pipeline_(start|complete)_phase",
        "phase_timing.py",
    ),
    ("stop_guard", "Stop", None, "stop_guard.py"),
]


def _config_path() -> Path:
    """Resolve the input TOML config path (env override or default)."""
    override = os.environ.get("PIPELINE_HOOKS_CONFIG")
    if override:
        return Path(override)
    return _HOOKS_DIR / "hooks-config.toml"


def _runtime_config_path() -> Path:
    """Resolve the runtime-config.json output path (env override or default)."""
    override = os.environ.get("PIPELINE_RUNTIME_CONFIG")
    if override:
        return Path(override)
    return _HOOKS_DIR / "runtime-config.json"


def _settings_path() -> Path:
    """Resolve the settings.local.json output path (env override or default)."""
    override = os.environ.get("PIPELINE_SETTINGS_PATH")
    if override:
        return Path(override)
    return _ROOT / ".claude" / "settings.local.json"


def main() -> None:
    """Generate runtime-config.json and merge hook entries into settings."""
    config_path = _config_path()
    runtime_config_path = _runtime_config_path()
    settings_path = _settings_path()

    # ── Load and parse TOML config ──────────────────────────────
    with config_path.open("rb") as fh:
        config: dict[str, Any] = tomllib.load(fh)

    # ── Write flattened runtime-config.json ──────────────────────
    runtime_config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
    print("Wrote runtime-config.json")

    # ── Build hook entries for settings.local.json ──────────────
    master = config.get("master", {})
    if not (isinstance(master, dict) and master.get("enabled")):
        print("Master switch is disabled — no hooks will be registered.")
        sys.exit(0)

    scripts_dir = _HOOKS_DIR / "scripts"

    hooks: list[dict[str, str]] = []
    for config_key, event, matcher, script_file in _HOOK_DEFS:
        hook_config = config.get(config_key, {})
        if not (isinstance(hook_config, dict) and hook_config.get("enabled")):
            continue

        cmd_path = (
            str(scripts_dir / script_file)
            .replace("\\", "/")
            .replace('"', '\\"')
        )
        entry: dict[str, str] = {
            "type": "command",
            "event": event,
            "command": f'python3 "{cmd_path}"',
        }
        if matcher is not None:
            entry["matcher"] = matcher
        hooks.append(entry)

    # ── Merge into settings.local.json ──────────────────────────
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    existing: dict[str, Any] = {}
    if settings_path.exists():
        try:
            parsed: Any = json.loads(settings_path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                existing = parsed
        except Exception:
            existing = {}

    # Preserve non-hook keys, replace the hooks array.
    existing["hooks"] = hooks

    settings_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    print(f"Wrote {len(hooks)} hooks to {settings_path}")


if __name__ == "__main__":
    main()
