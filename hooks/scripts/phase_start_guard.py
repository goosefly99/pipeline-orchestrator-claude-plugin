#!/usr/bin/env python3
"""Track phases started per session, warn or deny repeats — PreToolUse hook."""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from hooks.lib.common import load_config, read_stdin  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(
    os.environ.get("PIPELINE_RUNTIME_CONFIG")
    or (_ROOT / "hooks" / "runtime-config.json")
)

MAX_SANITIZED_SESSION_ID_LEN = 128
SESSION_FILE_TTL_MS = 24 * 60 * 60 * 1000  # 24 hours
SESSION_CLEANUP_MAX_FILES = 100


def sanitize_session_id(raw: Any) -> dict[str, Any]:
    """Sanitize a session_id string for safe use as a filename component.

    Any character outside [A-Za-z0-9_-] is considered unsafe. The sanitizer
    fails closed (caller should deny) if:
      - the input is not a string,
      - the input contains any char outside [A-Za-z0-9_-] (path traversal,
        null bytes, separators, etc.),
      - the (sanitized) result is empty — we never want to write to
        ``session-.json``,
      - the result exceeds MAX_SANITIZED_SESSION_ID_LEN characters.
    On success, the returned ``sanitized`` value is identical to the input
    (since any unsafe character would have already triggered a failure).
    """
    if not isinstance(raw, str):
        return {"ok": False, "reason": "session_id is not a string"}
    sanitized = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    if len(sanitized) == 0:
        return {"ok": False, "reason": "session_id is empty after sanitization"}
    if len(sanitized) > MAX_SANITIZED_SESSION_ID_LEN:
        return {
            "ok": False,
            "reason": (
                f"session_id exceeds {MAX_SANITIZED_SESSION_ID_LEN} "
                f"characters after sanitization"
            ),
        }
    if sanitized != raw:
        return {
            "ok": False,
            "reason": (
                "session_id contains disallowed characters "
                "(only [A-Za-z0-9_-] permitted)"
            ),
        }
    return {"ok": True, "sanitized": sanitized}


def cleanup_stale_session_files(track_dir: Path) -> None:
    """Remove stale session-tracking files older than SESSION_FILE_TTL_MS.

    Caps work at SESSION_CLEANUP_MAX_FILES per invocation so a huge $TMPDIR
    never makes this hook slow. Unlink errors are swallowed — this is
    best-effort housekeeping, not a correctness barrier.
    """
    if not track_dir.exists():
        return
    try:
        entries = os.listdir(track_dir)
    except Exception:
        return
    cutoff = time.time() - 86400
    processed = 0
    for name in entries:
        if processed >= SESSION_CLEANUP_MAX_FILES:
            break
        if not name.startswith("session-") or not name.endswith(".json"):
            continue
        full = track_dir / name
        try:
            mtime = full.stat().st_mtime
        except Exception:
            continue
        if mtime < cutoff:
            try:
                full.unlink()
            except Exception:
                # Ignore — another process may have just deleted it, or
                # permissions.
                pass
        processed += 1


def main() -> None:
    raw_input = read_stdin()

    try:
        default: dict[str, Any] = {"phase_session_isolation": {"warn_only": True}}
        config = {**default, **load_config(CONFIG_PATH)}

        guard_config: dict[str, Any] = config.get("phase_session_isolation") or {}
        warn_only = guard_config.get("warn_only") is not False

        hook_input: Any = json.loads(raw_input or "{}")
        raw_session_id = hook_input.get("session_id") or "unknown"
        tool_input: dict[str, Any] = hook_input.get("tool_input") or {}
        phase_name = (
            tool_input.get("phase") or tool_input.get("phase_name") or "unknown"
        )

        # Fail-closed on any session_id that can't be safely used as a file
        # component. This guards against path traversal (../../etc/passwd),
        # null bytes, absolute paths, and unbounded-length inputs.
        san = sanitize_session_id(raw_session_id)
        if not san["ok"]:
            sys.stdout.write(
                json.dumps(
                    {
                        "permissionDecision": "deny",
                        "deny_reason": (
                            f"phase-start-guard refused unsafe session_id: "
                            f"{san['reason']}"
                        ),
                    }
                )
            )
            sys.exit(0)
        session_id = san["sanitized"]

        # Track phases per session in TMPDIR
        track_dir = Path(tempfile.gettempdir()) / "pipeline-hooks-sessions"
        if not track_dir.exists():
            track_dir.mkdir(parents=True, exist_ok=True)

        # Best-effort cleanup of session-tracking files older than 24h so
        # $TMPDIR/pipeline-hooks-sessions/ doesn't grow unbounded across runs.
        # Scoped, rate-limited, and swallows errors — correctness never depends
        # on this firing.
        cleanup_stale_session_files(track_dir)

        session_file = track_dir / f"session-{session_id}.json"
        session_data: dict[str, Any] = {"phases_started": []}

        if session_file.exists():
            try:
                parsed: Any = json.loads(session_file.read_text(encoding="utf-8"))
                session_data = (
                    parsed if isinstance(parsed, dict) else {"phases_started": []}
                )
            except Exception:
                session_data = {"phases_started": []}

        phases_started: list[Any] = session_data.get("phases_started") or []
        already_started = phase_name in phases_started

        if already_started:
            if warn_only:
                sys.stdout.write(
                    json.dumps(
                        {
                            "permissionDecision": "allow",
                            "systemMessage": (
                                f'Phase "{phase_name}" was already started in '
                                f"this session. Consider using /clear between "
                                f"phases to reset context and reduce token "
                                f"costs."
                            ),
                        }
                    )
                )
            else:
                sys.stdout.write(
                    json.dumps(
                        {
                            "permissionDecision": "deny",
                            "deny_reason": (
                                f'Phase "{phase_name}" was already started in '
                                f"this session. Use /clear to reset context "
                                f"before starting a new phase."
                            ),
                        }
                    )
                )
        else:
            # Record this phase start
            phases_started.append(phase_name)
            session_data["phases_started"] = phases_started
            session_file.write_text(
                json.dumps(session_data, indent=2), encoding="utf-8"
            )

            sys.stdout.write(json.dumps({"permissionDecision": "allow"}))
    except Exception:
        sys.stdout.write(json.dumps({"permissionDecision": "allow"}))


if __name__ == "__main__":
    main()
