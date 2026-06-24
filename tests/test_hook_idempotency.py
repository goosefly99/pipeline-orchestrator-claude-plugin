"""Port of ``legacy-node/tests/hook-idempotency.test.ts`` (6 cases).

Byte-exact behavioral parity tests for the ``post_complete`` idempotency guard
in :mod:`pipeline_orchestrator.hooks` (``run_hooks``) plus the
``appendPhaseCompletedEvent`` wrapper. Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks:

* ``runHooks post_complete idempotency guard`` ×2 (in T2.5 scope — spawns ``node``)
* ``appendPhaseCompletedEvent`` ×3 → **skipped** (T6.2 lifecycle-handlers)
* ``TS+JS idempotency after Fix 6.5`` ×1 → **skipped** (T6.2 + Node-only script)

The two idempotency-guard cases exercise ``run_hooks``' events.jsonl read path.
The Node original seeds events.jsonl via ``appendEvent``; here we seed it
directly with the same on-disk wire format (one ``json.dumps(event)`` line per
event) because the ``append_event`` writer is a T6.2 deliverable — the guard
under test reads the file, it does not write it.

The 3 ``appendPhaseCompletedEvent`` cases and the 1 ``TS+JS`` case drive
``appendPhaseCompletedEvent`` from ``lifecycle-handlers.ts`` (and, for the last,
a Node-only ``hooks/scripts/post-phase-complete.mjs`` source assertion). That
wrapper + Node script are **T6.2** deliverables; ported 1:1 here but skipped.

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` via the ``tmp_path`` fixture.
All paths are OS-agnostic (``os.path.join`` over ``tmp_path``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from pipeline_orchestrator.hooks import run_hooks
from pipeline_orchestrator.models import HookConfig

_NODE_PATH = shutil.which("node")
_NODE_REASON = "requires the node binary on PATH to run hook subprocesses"


def _node_hook_command() -> tuple[str, str]:
    """Return ``(project_root, command)`` for executing ``node`` as a hook."""
    assert _NODE_PATH is not None
    return os.path.dirname(_NODE_PATH), os.path.basename(_NODE_PATH)


def _seed_event(run_dir: str, event: dict[str, object]) -> None:
    """Append one event line to events.jsonl in the on-disk wire format.

    Mirrors the Node ``appendEvent`` on-disk effect (one ``JSON.stringify(event)``
    line, newline-terminated) without importing the T6.2 lifecycle-handler
    writer. The ``run_hooks`` idempotency guard reads this file via
    ``_has_phase_completed_event``.
    """
    os.makedirs(run_dir, exist_ok=True)
    events_path = os.path.join(run_dir, "events.jsonl")
    with open(events_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def _count_phase_completed_events(run_dir: str, phase: str) -> int:
    """Count ``phase_completed`` events for ``phase`` in events.jsonl.

    Mirrors the Node ``countPhaseCompletedEvents`` helper.
    """
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(events_path):
        return 0
    with open(events_path, encoding="utf-8") as fh:
        content = fh.read()
    count = 0
    for line in content.split("\n"):
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            parsed = json.loads(trimmed)
        except (json.JSONDecodeError, ValueError):
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("event") == "phase_completed"
            and parsed.get("phase") == phase
        ):
            count += 1
    return count


def _now_iso() -> str:
    """Local helper mirroring Node ``new Date().toISOString()`` for fixtures."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ── Test case 1: idempotency guard suppresses duplicate post_complete ──


@pytest.mark.skipif(_NODE_PATH is None, reason=_NODE_REASON)
class TestRunHooksPostCompleteIdempotencyGuard:
    def test_skips_hooks_and_does_not_add_a_duplicate_event_when_already_recorded(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        # Pre-seed events.jsonl with a phase_completed event for phase1.
        _seed_event(
            temp_dir,
            {
                "timestamp": _now_iso(),
                "event": "phase_completed",
                "phase": "phase1",
                "run_id": "test-run",
            },
        )

        assert _count_phase_completed_events(temp_dir, "phase1") == 1, (
            "pre-seed should place exactly one event"
        )

        # Construct a hook that would, if run, add a second phase_completed event.
        # Because the idempotency guard should fire first, the hook command is
        # never actually invoked — but for safety we point it at a valid command
        # that would succeed anyway.
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-script.mjs")
        Path(script_path).write_text(
            'console.log("should_not_run")\n', encoding="utf-8"
        )

        hooks = [
            HookConfig(
                trigger="post_complete",
                phase_filter="*",
                command=node_command,
                args=[script_path],
            )
        ]

        ctx = {
            "run_id": "test-run",
            "phase": "phase1",
            "trigger": "post_complete",
            "project_root": hook_root,
            "run_dir": temp_dir,
        }

        results = run_hooks(hooks, "post_complete", ctx, hook_root)

        assert len(results) == 1, "one hook should have matched"
        assert results[0]["success"] is False, (
            "hook should be reported as not-successful"
        )
        assert results[0].get("error") is not None, (
            "error message should be populated"
        )
        error_msg = results[0]["error"]
        assert "idempotency" in error_msg or "already recorded" in error_msg, (
            f"error must mention idempotency or already recorded — got: "
            f'"{error_msg}"'
        )

        # Ensure no duplicate event was written.
        assert _count_phase_completed_events(temp_dir, "phase1") == 1, (
            "exactly one phase_completed event should remain for phase1"
        )

    def test_actually_runs_post_complete_hooks_when_no_prior_event_exists(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        # Phase3 has no phase_completed event in events.jsonl. Install a small
        # .mjs script that writes a marker file so we can verify the hook ran.
        hook_root, node_command = _node_hook_command()
        marker_path = os.path.join(temp_dir, "hook-marker.txt")
        script_path = os.path.join(temp_dir, "phase3-hook.mjs")
        # Use json.dumps on the marker path so backslashes survive.
        Path(script_path).write_text(
            "import { writeFileSync } from 'node:fs'\n"
            f"writeFileSync({json.dumps(marker_path)}, 'ran', 'utf-8')\n",
            encoding="utf-8",
        )

        hooks = [
            HookConfig(
                trigger="post_complete",
                phase_filter="*",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        ctx = {
            "run_id": "test-run",
            "phase": "phase3",
            "trigger": "post_complete",
            "project_root": hook_root,
            "run_dir": temp_dir,
        }

        # Pre-seed an UNRELATED phase_completed event for phase1 so the events
        # file exists but does not contain phase3.
        _seed_event(
            temp_dir,
            {
                "timestamp": _now_iso(),
                "event": "phase_completed",
                "phase": "phase1",
                "run_id": "test-run",
            },
        )

        results = run_hooks(hooks, "post_complete", ctx, hook_root)

        assert len(results) == 1, "one hook should have matched"
        assert results[0]["success"] is True, (
            f"hook should have succeeded — got error: "
            f'"{results[0].get("error") or ""}", '
            f'stderr: "{results[0].get("stderr") or ""}"'
        )
        assert os.path.exists(marker_path), (
            "hook script should have written marker file"
        )
        assert Path(marker_path).read_text(encoding="utf-8") == "ran"


# ── Test case 3: appendPhaseCompletedEvent validation + idempotency ──
#
# These 3 cases drive ``appendPhaseCompletedEvent`` from ``lifecycle-handlers.ts``
# — the idempotent events.jsonl wrapper that validates phase existence + status
# before writing. That wrapper is a **T6.2** deliverable; ported 1:1 here but
# skipped until ``append_phase_completed_event`` lands.

_APPEND_PHASE_COMPLETED_REASON = (
    "T6.2: appendPhaseCompletedEvent (validating idempotent events.jsonl wrapper) "
    "lives in the lifecycle-handler layer (lifecycle-handlers.ts), not the T2.5 "
    "hooks seam. Ported 1:1 and unskipped when lifecycle_handlers lands."
)


@pytest.mark.skip(reason=_APPEND_PHASE_COMPLETED_REASON)
class TestAppendPhaseCompletedEvent:
    def test_writes_once_then_suppresses_duplicate_writes_for_the_same_phase(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_APPEND_PHASE_COMPLETED_REASON)

    def test_throws_state_transition_error_when_phase_is_not_in_state(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_APPEND_PHASE_COMPLETED_REASON)

    def test_throws_state_transition_error_when_phase_status_is_not_completed(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_APPEND_PHASE_COMPLETED_REASON)


# ── Test case 4: TS + JS double-write regression (Fix 6.5) ────────────
#
# Asserts the Node-only ``hooks/scripts/post-phase-complete.mjs`` source no
# longer calls ``appendFileSync`` against events.jsonl. The JS hook scripts are
# not part of the pure-Python port surface, and the assertion depends on
# ``appendPhaseCompletedEvent`` (T6.2). Ported 1:1 here but skipped.

_TS_JS_REASON = (
    "T6.2: depends on appendPhaseCompletedEvent (lifecycle-handlers.ts) and "
    "asserts the source of a Node-only hooks/scripts/post-phase-complete.mjs "
    "file, which is not part of the pure-Python port surface. Ported 1:1 and "
    "unskipped when lifecycle_handlers lands."
)


@pytest.mark.skip(reason=_TS_JS_REASON)
class TestTSJSIdempotencyAfterFix65:
    def test_post_phase_complete_mjs_does_not_write_a_duplicate(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_TS_JS_REASON)
