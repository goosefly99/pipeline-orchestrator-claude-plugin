"""Shared fixtures and helpers for the harness-hook pytest suite.

Ports the Node ``run-tests.mjs`` harness (44 cases, 12 sections) to pytest.
Every hook is invoked as a subprocess exactly the way Claude Code invokes it:
``python3 <abs script path>`` with the hook JSON on stdin, reading a single
JSON object back from stdout.

All paths are resolved relative to this file so the suite is location
independent. Tests are kept hermetic by pointing the hooks' env seams
(``PIPELINE_MCP_DATA_DIR``, ``PIPELINE_RUNTIME_CONFIG``,
``PIPELINE_HOOK_LOGS_DIR``, ``TMPDIR``) at per-test ``tmp_path`` dirs via the
``hook_env`` fixture.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

import pytest

# ── Path anchors (relative to this test file) ───────────────────
HOOKS_DIR: Path = Path(__file__).resolve().parents[1]
SCRIPTS_DIR: Path = HOOKS_DIR / "scripts"
FIXTURES_DIR: Path = Path(__file__).resolve().parent / "fixtures"


class HookResult(NamedTuple):
    """The outcome of one hook subprocess invocation."""

    exit_code: int
    stdout: str
    parsed: Any


def _resolve_input(payload: str | dict[str, Any]) -> str:
    """Turn a ``run_hook`` payload into the raw stdin string for the hook.

    Rules:
      * ``dict``  -> ``json.dumps(payload)``
      * ``str`` ending in ``.json`` -> contents of ``FIXTURES_DIR/<payload>``
      * any other ``str`` -> the raw string itself (used for oversized stdin)
    """
    if isinstance(payload, dict):
        return json.dumps(payload)
    if payload.endswith(".json"):
        return (FIXTURES_DIR / payload).read_text(encoding="utf-8")
    return payload


def run_hook(
    script: str,
    payload: str | dict[str, Any],
    *,
    env: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> HookResult:
    """Invoke a hook script as a subprocess and capture its result.

    ``script`` is a filename under ``hooks/scripts/`` (e.g.
    ``"dag_guard.py"``). ``payload`` is resolved by :func:`_resolve_input`.
    ``env`` is merged over ``os.environ`` so callers only need to specify the
    hermetic overrides. ``parsed`` is the JSON object on stdout, or ``None``
    when stdout is empty (the legacy harness treats both as valid).
    """
    full_env: dict[str, str] = {**os.environ, **(env or {})}
    stdin_data = _resolve_input(payload)
    completed: subprocess.CompletedProcess[str] = subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script)],
        input=stdin_data,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=full_env,
        check=False,
    )
    stripped = completed.stdout.strip()
    parsed: Any = json.loads(stripped) if stripped else None
    return HookResult(
        exit_code=completed.returncode,
        stdout=completed.stdout,
        parsed=parsed,
    )


def _default_config() -> dict[str, Any]:
    """Return the baseline runtime-config dict mirroring legacy ``setupConfig``."""
    return {
        "master": {"enabled": True},
        "run": {"id": "", "label": "test"},
        "session_start_clear": {"enabled": True},
        "send_message_guard": {
            "enabled": True,
            "mode": "ask",
            "deny_reason": "Test deny reason.",
        },
        "token_budget_guard": {
            "enabled": True,
            "max_extra_usage_usd": 5.0,
            "warn_at_pct": 80,
        },
        "phase_session_isolation": {"enabled": True, "warn_only": True},
        "dag_guard": {"enabled": True},
        "artifact_gate": {"enabled": True},
        "file_read_version_guard": {
            "enabled": True,
            "present_version_in_development": "v0.4.0",
            "previous_versions": ["v0.3.0", "v0.2.0"],
        },
        "post_phase_complete": {"enabled": True},
        "phase_context_inject": {"enabled": True},
        "phase_timing": {"enabled": True},
        "stop_guard": {"enabled": True},
    }


def write_runtime_config(
    path: str | Path,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Write the merged runtime-config dict to ``path`` and return it.

    The base config mirrors the legacy ``setupConfig`` defaults; ``overrides``
    are applied shallowly (top-level key replacement, matching the JS spread).
    """
    config = _default_config()
    if overrides:
        config.update(overrides)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def seed_run_state(
    data_dir: str | Path,
    run_dir: str,
    state: dict[str, Any],
) -> Path:
    """Seed ``<data_dir>/runs/<run_dir>/run-state.json`` and return its path.

    The state's ``updated_at`` is forced to a future timestamp so
    ``find_latest_run_state`` always ranks this seeded state ahead of any other
    run that might exist on disk, making the dependent tests deterministic.
    """
    future = (datetime.now(UTC) + timedelta(seconds=1000)).isoformat()
    state = {**state, "updated_at": future}
    run_path = Path(data_dir) / "runs" / run_dir
    run_path.mkdir(parents=True, exist_ok=True)
    state_path = run_path / "run-state.json"
    state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return state_path


@pytest.fixture
def hook_env(tmp_path: Path) -> dict[str, str]:
    """Return a hermetic env mapping the hooks' four env seams into ``tmp_path``.

    Creates the backing directories so the hooks never fall back to repo-root
    defaults. ``TMPDIR`` is honoured by ``tempfile.gettempdir()`` (which
    phase_start_guard uses) on every platform.
    """
    data_dir = tmp_path / "data"
    logs_dir = tmp_path / "logs"
    tmp_dir = tmp_path / "tmp"
    runtime_config = tmp_path / "runtime-config.json"
    for directory in (data_dir, logs_dir, tmp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return {
        "PIPELINE_MCP_DATA_DIR": str(data_dir),
        "PIPELINE_RUNTIME_CONFIG": str(runtime_config),
        "PIPELINE_HOOK_LOGS_DIR": str(logs_dir),
        "TMPDIR": str(tmp_dir),
    }
