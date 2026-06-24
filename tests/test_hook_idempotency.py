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

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.hooks import run_hooks
from pipeline_orchestrator.models import HookConfig, PhaseState, RunState
from pipeline_orchestrator.tools.lifecycle_tools import (
    append_phase_completed_event,
)

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
# These 3 cases drive ``append_phase_completed_event`` from ``lifecycle_tools.py``
# — the idempotent events.jsonl wrapper that validates phase existence + status
# before writing. Unskipped at T6.2 when ``append_phase_completed_event`` landed;
# ported 1:1 against the TS oracle.


def _make_phase(name: str, status: str) -> PhaseState:
    """Build a PhaseState with started/completed stamps (Node ``makePhase``)."""
    return PhaseState(
        phase_name=name,
        status=status,  # type: ignore[arg-type]
        input_artifacts=[],
        output_artifacts=[],
        retry_count=0,
        started_at=_now_iso() if status != "pending" else None,
        completed_at=_now_iso() if status == "completed" else None,
    )


def _make_run_state(temp_dir: str, phases: dict[str, str]) -> RunState:
    """Build a RunState from a ``{name: status}`` map (Node ``makeRunState``)."""
    phase_map: dict[str, PhaseState] = {}
    for name, status in phases.items():
        phase_map[name] = _make_phase(name, status)
    return RunState(
        run_id="test-run",
        pipeline_version="1.0.0",
        created_at=_now_iso(),
        updated_at=_now_iso(),
        status="running",
        config_path=temp_dir,
        phases=phase_map,
        available_artifacts=[],
    )


class TestAppendPhaseCompletedEvent:
    def test_writes_once_then_suppresses_duplicate_writes_for_the_same_phase(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = _make_run_state(temp_dir, {"phaseA": "completed"})

        first_write = append_phase_completed_event(temp_dir, state, "phaseA")
        assert first_write is True
        assert _count_phase_completed_events(temp_dir, "phaseA") == 1

        second_write = append_phase_completed_event(temp_dir, state, "phaseA")
        assert second_write is False
        assert _count_phase_completed_events(temp_dir, "phaseA") == 1

    def test_throws_state_transition_error_when_phase_is_not_in_state(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = _make_run_state(temp_dir, {"phaseA": "completed"})

        with pytest.raises(PipelineError) as exc_info:
            append_phase_completed_event(temp_dir, state, "phaseB")
        assert exc_info.value.error_class == ErrorClass.state_transition_error
        assert "phaseB" in exc_info.value.message

    def test_throws_state_transition_error_when_phase_status_is_not_completed(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = _make_run_state(temp_dir, {"phaseC": "in_progress"})

        with pytest.raises(PipelineError) as exc_info:
            append_phase_completed_event(temp_dir, state, "phaseC")
        assert exc_info.value.error_class == ErrorClass.state_transition_error
        assert "in_progress" in exc_info.value.message


# ── Test case 4: TS + JS double-write regression (Fix 6.5) ────────────
#
# Asserts the Node ``hooks/scripts/post-phase-complete.mjs`` source no longer
# calls ``appendFileSync`` against events.jsonl (Fix 6.5). The ``.mjs`` hook
# scripts are not part of the pure-Python port surface, so the source path is
# resolved under ``legacy-node/`` (the frozen Node tree) rather than the Node
# ``process.cwd()``. The ``append_phase_completed_event`` half is the real T6.2
# wrapper. Unskipped at T6.2; ported 1:1.


class TestTSJSIdempotencyAfterFix65:
    def test_post_phase_complete_mjs_does_not_write_a_duplicate(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = _make_run_state(temp_dir, {"curation": "completed"})
        run_dir = temp_dir

        # Write one phase_completed via the wrapper.
        append_phase_completed_event(run_dir, state, "curation")
        assert _count_phase_completed_events(run_dir, "curation") == 1

        # Assert the frozen Node post-phase-complete.mjs source does NOT append
        # directly to events.jsonl (Fix 6.5). Resolved under legacy-node/.
        repo_root = Path(__file__).resolve().parent.parent
        post_path = (
            repo_root / "legacy-node" / "hooks" / "scripts" / "post-phase-complete.mjs"
        )
        src = post_path.read_text(encoding="utf-8")
        assert "appendFileSync(eventsPath" not in src
        assert "appendFileSync" not in src

        # Count remains 1 (the wrapper already wrote it).
        assert _count_phase_completed_events(run_dir, "curation") == 1
