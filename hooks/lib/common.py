"""Shared helpers for Claude Code harness hook scripts.

All scripts under ``hooks/scripts/`` import from this module. Do not add
side-effects at import time — every helper must be pure/lazy so scripts can
import only what they need without paying startup cost for unused helpers.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Maximum bytes of stdin a hook will accept. Inputs larger than this trigger a
# fail-open response and a warning on stderr.
MAX_STDIN_BYTES: int = 10 * 1024 * 1024  # 10 MB

_CHUNK_SIZE: int = 65536


def read_stdin() -> str:
    """Read all of stdin as a UTF-8 string with a 10 MB size cap.

    On cap exceeded, writes a warning to stderr, emits a fail-open response
    (``{"permissionDecision": "allow"}``) on stdout, and exits the process with
    code 0. Callers do not need to handle the overflow case.

    Returns ``''`` when stdin is empty or already closed.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = sys.stdin.buffer.read(_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_STDIN_BYTES:
            sys.stderr.write(
                f"[hooks/common] stdin exceeded {MAX_STDIN_BYTES} bytes "
                f"— failing open\n",
            )
            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
            sys.exit(0)
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load a runtime-config JSON object from the given path.

    Returns an empty dict on a missing file or any read/JSON error so callers
    can apply per-hook defaults via ``{**defaults, **load_config(path)}``. If
    the parsed top-level value is not a dict, also returns an empty dict.
    """
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    result: dict[str, Any] = data
    return result


def _parse_updated_at(value: Any) -> datetime | None:
    """Parse an ISO-8601 ``updated_at`` string, or return ``None`` if invalid.

    A trailing ``Z`` is normalised to ``+00:00`` so ``fromisoformat`` accepts
    it. Anything unparseable or non-string yields ``None`` (treated as
    not-newer for ranking).
    """
    if not isinstance(value, str):
        return None
    normalised = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        return None


def find_latest_run_state(runs_dir: str | Path, include_dir: bool = False) -> Any:
    """Return the most-recently-updated run state under ``runs_dir``.

    Scans ``runs_dir/<subdir>/run-state.json``. Among readable, parseable
    states, returns the one whose ``updated_at`` parses to the greatest
    timestamp. States with a missing or unparseable ``updated_at`` are skipped
    for ranking.

    When ``include_dir`` is ``False``, returns the state dict (or ``None`` when
    no state is found or ``runs_dir`` is missing). When ``include_dir`` is
    ``True``, returns ``{"state": <dict|None>, "dir": <str|None>}``.
    """
    null_result: Any = {"state": None, "dir": None} if include_dir else None

    runs_path = Path(runs_dir)
    if not runs_path.exists():
        return null_result

    try:
        entries = list(os.scandir(runs_path))
    except OSError:
        return null_result

    latest_state: dict[str, Any] | None = None
    latest_dir: str | None = None
    latest_time: datetime | None = None

    for entry in entries:
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        state_path = Path(entry.path) / "run-state.json"
        if not state_path.exists():
            continue
        try:
            parsed: Any = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        state: dict[str, Any] = parsed
        updated_at = _parse_updated_at(state.get("updated_at"))
        if updated_at is None:
            continue
        if latest_time is None or updated_at > latest_time:
            latest_time = updated_at
            latest_state = state
            latest_dir = entry.path

    if include_dir:
        return {"state": latest_state, "dir": latest_dir}
    return latest_state
