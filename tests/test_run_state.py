"""Port of ``legacy-node/tests/run-state.test.ts`` (90 cases) + 1 added case.

Byte-exact behavioral parity tests for the run-state machine
(``pipeline_orchestrator.run_state``). Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks. The transition error/recovery strings are pinned verbatim against the
source.

One case beyond the 90 is added (``TestAtomicRename.test_windows_permission_error
_fallback_copies_and_removes_tmp``) to exercise the Windows cross-volume
``PermissionError`` → copy+remove fallback in ``atomic_rename``, per M1.

All paths are OS-agnostic: directories come from the ``tmp_path`` fixture and
expected paths are built with ``os.path.join`` — never hardcoded POSIX slashes.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.models import ArtifactRef
from pipeline_orchestrator.run_state import (
    STALE_PHASE_THRESHOLD_MS,
    add_artifact,
    atomic_rename,
    complete_phase,
    compute_recommended_action,
    compute_run_warnings,
    fail_phase,
    get_artifact_types,
    get_completed_phases,
    get_in_progress_phases,
    get_skipped_phases,
    init_run,
    load_run_state,
    recover_run,
    resolve_run_write_dir,
    retry_phase,
    skip_phase,
    start_phase,
    validate_run_state_integrity,
)
from pipeline_orchestrator.storage import (
    get_run_data_dir,
    sanitize_run_name,
    sanitize_run_timestamp,
)


def _now_iso() -> str:
    """Local helper mirroring Node ``new Date().toISOString()`` for fixtures."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _iso_to_ms(iso: str) -> float:
    """Mirror JS ``new Date(iso).getTime()`` (epoch milliseconds)."""
    candidate = iso[:-1] + "+00:00" if iso.endswith("Z") else iso
    return datetime.fromisoformat(candidate).timestamp() * 1000


# ── initRun ──────────────────────────────────────────────────────────


class TestInitRun:
    def test_creates_new_run_state_with_all_phases_pending(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        phase_names = ["discovery", "curation", "synthesis"]
        state = init_run("test-run-1", "1.0.0", phase_names, temp_dir)

        assert state.run_id == "test-run-1"
        assert state.status == "initialized"
        assert len(state.phases) == 3
        assert state.phases["discovery"].status == "pending"
        assert state.phases["curation"].status == "pending"

    def test_persists_state_to_disk(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        init_run("test-run-2", "1.0.0", ["discovery"], temp_dir)
        assert os.path.exists(os.path.join(temp_dir, "run-state.json"))

    def test_sets_run_data_dir_when_params_present(self, tmp_path: Path) -> None:
        base_dir = os.path.join(str(tmp_path), "test-base")
        state_dir = os.path.join(base_dir, "runs", "run-001")
        state = init_run(
            "run-001",
            "v1",
            ["phase-a"],
            state_dir,
            {
                "run_name": "My Test Run",
                "run_directory_timestamp": "2026-04-10T12:34:56.789Z",
            },
        )

        assert state.run_data_dir, "run_data_dir should be defined"
        runs_prefix = os.path.join(base_dir, "runs") + os.sep
        assert state.run_data_dir.startswith(runs_prefix)
        assert "my-test-run" in state.run_data_dir
        assert "2026-04-10T12-34-56-789Z" in state.run_data_dir

    def test_leaves_run_data_dir_undefined_and_warns_when_absent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        state_dir = os.path.join(str(tmp_path), "test-base", "runs", "run-002")
        state = init_run("run-002", "v1", ["phase-a"], state_dir)
        assert state.run_data_dir is None
        captured = capsys.readouterr()
        assert "run-002" in captured.err
        assert "parameterization inactive" in captured.err


# ── startPhase ───────────────────────────────────────────────────────


class TestStartPhase:
    def test_marks_phase_as_in_progress(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        updated = start_phase(state, "discovery", temp_dir)

        assert updated.phases["discovery"].status == "in_progress"
        assert updated.phases["discovery"].started_at
        assert updated.status == "running"

    def test_throws_if_phase_does_not_exist(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        with pytest.raises(PipelineError, match="not found"):
            start_phase(state, "nonexistent", temp_dir)

    def test_rejects_starting_already_in_progress_phase(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot start phase "discovery": status is "in_progress" '
            r'\(expected "pending"\)',
        ):
            start_phase(state, "discovery", temp_dir)

    def test_rejects_starting_completed_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot start phase "discovery": status is "completed" '
            r'\(expected "pending"\)',
        ):
            start_phase(state, "discovery", temp_dir)


# ── completePhase ────────────────────────────────────────────────────


class TestCompletePhase:
    def test_marks_phase_as_completed(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        updated = complete_phase(state, "discovery", temp_dir)

        assert updated.phases["discovery"].status == "completed"
        assert updated.phases["discovery"].completed_at

    def test_rejects_completing_pending_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot complete phase "discovery": status is "pending" '
            r'\(expected "in_progress"\)',
        ):
            complete_phase(state, "discovery", temp_dir)


# ── failPhase ────────────────────────────────────────────────────────


class TestFailPhase:
    def test_marks_phase_as_failed_with_error(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        updated = fail_phase(state, "discovery", "API timeout", temp_dir)

        assert updated.phases["discovery"].status == "failed"
        assert updated.phases["discovery"].error == "API timeout"
        assert updated.status == "failed"

    def test_rejects_failing_pending_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot fail phase "discovery": status is "pending" '
            r'\(expected "in_progress"\)',
        ):
            fail_phase(state, "discovery", "error", temp_dir)


# ── retryPhase ───────────────────────────────────────────────────────


class TestRetryPhase:
    def test_failed_phase_back_to_pending_and_increments_retry(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "API timeout", temp_dir)

        assert state.phases["discovery"].status == "failed"
        assert state.phases["discovery"].retry_count == 0

        updated = retry_phase(state, "discovery", temp_dir)
        assert updated.phases["discovery"].status == "pending"
        assert updated.phases["discovery"].error is None
        assert updated.phases["discovery"].retry_count == 1

    def test_increments_retry_count_on_each_successive_retry(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "first failure", temp_dir)
        state = retry_phase(state, "discovery", temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "second failure", temp_dir)
        state = retry_phase(state, "discovery", temp_dir)

        assert state.phases["discovery"].retry_count == 2
        assert state.phases["discovery"].status == "pending"

    def test_persists_state_to_disk_after_retry(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "oops", temp_dir)
        retry_phase(state, "discovery", temp_dir)

        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "pending"
        assert loaded.phases["discovery"].retry_count == 1
        assert loaded.phases["discovery"].error is None

    def test_throws_if_phase_does_not_exist(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        with pytest.raises(PipelineError, match="not found"):
            retry_phase(state, "nonexistent", temp_dir)

    def test_rejects_retrying_pending_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot retry phase "discovery": status is "pending" '
            r'\(expected "failed"\)',
        ):
            retry_phase(state, "discovery", temp_dir)

    def test_rejects_retrying_in_progress_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot retry phase "discovery": status is "in_progress" '
            r'\(expected "failed"\)',
        ):
            retry_phase(state, "discovery", temp_dir)

    def test_rejects_retrying_completed_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot retry phase "discovery": status is "completed" '
            r'\(expected "failed"\)',
        ):
            retry_phase(state, "discovery", temp_dir)


# ── skipPhase ────────────────────────────────────────────────────────


class TestSkipPhase:
    def test_marks_phase_as_skipped(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        updated = skip_phase(state, "discovery", temp_dir)
        assert updated.phases["discovery"].status == "skipped"

    def test_rejects_skipping_in_progress_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        with pytest.raises(
            PipelineError,
            match=r'Cannot skip phase "discovery": status is "in_progress" '
            r'\(expected "pending"\)',
        ):
            skip_phase(state, "discovery", temp_dir)


# ── addArtifact ──────────────────────────────────────────────────────


class TestAddArtifact:
    def test_adds_artifact_ref_to_run_state(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="collections/raw/test.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )

        assert len(state.available_artifacts) == 1
        assert state.available_artifacts[0].type == "raw-collection"

    def test_stores_version_and_parent_artifact_when_provided(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test", "1.0.0", ["discovery"], temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="design-spec",
                path="specs/v2.json",
                phase="discovery",
                created_at=_now_iso(),
                version=2,
                parent_artifact="specs/v1.json",
            ),
            temp_dir,
        )

        ref = state.available_artifacts[0]
        assert ref.version == 2
        assert ref.parent_artifact == "specs/v1.json"


# ── loadRunState ─────────────────────────────────────────────────────


class TestLoadRunState:
    def test_loads_persisted_state_from_disk(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        init_run("persisted-run", "1.0.0", ["discovery", "curation"], temp_dir)
        loaded = load_run_state(temp_dir)

        assert loaded
        assert loaded.run_id == "persisted-run"
        assert len(loaded.phases) == 2

    def test_returns_none_if_no_state_file(self, tmp_path: Path) -> None:
        loaded = load_run_state(os.path.join(str(tmp_path), "nonexistent"))
        assert loaded is None

    def test_normalizes_version_to_1_for_artifacts_missing_field(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        legacy_state = {
            "run_id": "legacy-run",
            "pipeline_version": "1.0.0",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "running",
            "phases": {
                "discovery": {
                    "phase_name": "discovery",
                    "status": "in_progress",
                    "input_artifacts": [],
                    "output_artifacts": [],
                    "retry_count": 0,
                }
            },
            "available_artifacts": [
                {
                    "type": "raw-collection",
                    "path": "test.json",
                    "phase": "discovery",
                    "created_at": "2026-01-01T00:00:00.000Z",
                }
            ],
            "config_path": temp_dir,
        }
        Path(os.path.join(temp_dir, "run-state.json")).write_text(
            json.dumps(legacy_state), encoding="utf-8"
        )

        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.available_artifacts[0].version == 1
        assert loaded.available_artifacts[0].parent_artifact is None

    def test_preserves_existing_version_and_parent_on_load(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state_with_version = {
            "run_id": "versioned-run",
            "pipeline_version": "1.0.0",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "running",
            "phases": {
                "design_synthesis": {
                    "phase_name": "design_synthesis",
                    "status": "in_progress",
                    "input_artifacts": [],
                    "output_artifacts": [],
                    "retry_count": 0,
                }
            },
            "available_artifacts": [
                {
                    "type": "design-spec",
                    "path": "specs/v2.json",
                    "phase": "design_synthesis",
                    "created_at": "2026-01-01T00:00:00.000Z",
                    "version": 2,
                    "parent_artifact": "specs/v1.json",
                }
            ],
            "config_path": temp_dir,
        }
        Path(os.path.join(temp_dir, "run-state.json")).write_text(
            json.dumps(state_with_version), encoding="utf-8"
        )

        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.available_artifacts[0].version == 2
        assert loaded.available_artifacts[0].parent_artifact == "specs/v1.json"


# ── computeRecommendedAction ─────────────────────────────────────────


class TestComputeRecommendedAction:
    def test_returns_continue_when_phase_in_progress(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r1", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        action = compute_recommended_action(state, ["curation"])
        assert action.action == "continue"
        assert action.phase == "discovery"
        assert "discovery" in action.reason
        assert "in progress" in action.reason

    def test_returns_start_phase_with_first_next_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r2", "1.0.0", ["discovery", "curation"], temp_dir)
        action = compute_recommended_action(state, ["discovery", "curation"])
        assert action.action == "start_phase"
        assert action.phase == "discovery"
        assert "discovery" in action.reason

    def test_returns_validate_when_completed_and_no_next(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r3", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        assert state.status == "completed"
        action = compute_recommended_action(state, [])
        assert action.action == "validate"
        assert "validate" in action.reason.lower()

    def test_returns_review_when_no_next_and_not_completed(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r4", "1.0.0", ["discovery"], temp_dir)
        action = compute_recommended_action(state, [])
        assert action.action == "review"
        assert (
            "blocked" in action.reason.lower() or "failed" in action.reason.lower()
        )

    def test_prefers_in_progress_over_available_next(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r5", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        action = compute_recommended_action(state, ["curation"])
        assert action.action == "continue"
        assert action.phase == "discovery"

    def test_returns_retry_when_phase_failed(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r6", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "API timeout", temp_dir)
        action = compute_recommended_action(state, [])
        assert action.action == "retry"
        assert action.phase == "discovery"
        assert "discovery" in action.reason
        assert "retry" in action.reason.lower()

    def test_does_not_return_retry_when_no_failure(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r7", "1.0.0", ["discovery", "curation"], temp_dir)
        action = compute_recommended_action(state, ["discovery"])
        assert action.action != "retry"
        assert action.action == "start_phase"

    def test_prefers_in_progress_over_failed_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r8", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state.phases["curation"].status = "failed"
        state.phases["curation"].error = "earlier failure"
        action = compute_recommended_action(state, [])
        assert action.action == "continue"
        assert action.phase == "discovery"


# ── computeRunWarnings ───────────────────────────────────────────────


class TestComputeRunWarnings:
    def test_returns_empty_when_no_warnings(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-clean", "1.0.0", ["discovery"], temp_dir)
        warnings = compute_run_warnings(state, set())
        assert warnings == []

    def test_flags_in_progress_phase_exceeding_stale_threshold(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-stale", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        two_hours_ago_ms = (
            datetime.now(UTC).timestamp() * 1000 - 7_200_000
        )
        state.phases["discovery"].started_at = _ms_to_iso(two_hours_ago_ms)
        warnings = compute_run_warnings(state, set())
        assert len(warnings) == 1
        assert "discovery" in warnings[0]
        assert "1 hour" in warnings[0]

    def test_does_not_flag_in_progress_under_stale_threshold(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-fresh", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        warnings = compute_run_warnings(state, set())
        assert warnings == []

    def test_flags_failed_optional_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-opt-fail", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "curation", temp_dir)
        state = fail_phase(state, "curation", "API timeout", temp_dir)
        warnings = compute_run_warnings(state, {"curation"})
        assert len(warnings) == 1
        assert "curation" in warnings[0]
        assert "API timeout" in warnings[0]
        assert "optional" in warnings[0].lower()

    def test_does_not_flag_failed_non_optional_phase(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-req-fail", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "hard failure", temp_dir)
        warnings = compute_run_warnings(state, set())
        assert warnings == []

    def test_respects_injected_now_ms(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-now", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        started_at = state.phases["discovery"].started_at
        assert started_at is not None
        started_ms = _iso_to_ms(started_at)
        now_ms = started_ms + STALE_PHASE_THRESHOLD_MS + 1000
        warnings = compute_run_warnings(state, set(), now_ms)
        assert len(warnings) == 1

    def test_produces_both_kinds_of_warnings(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("r-both", "1.0.0", ["discovery", "curation"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        two_hours_ago_ms = (
            datetime.now(UTC).timestamp() * 1000 - 7_200_000
        )
        state.phases["discovery"].started_at = _ms_to_iso(two_hours_ago_ms)
        state = start_phase(state, "curation", temp_dir)
        state = fail_phase(state, "curation", "flaky", temp_dir)
        warnings = compute_run_warnings(state, {"curation"})
        assert len(warnings) == 2


# ── atomicRename ─────────────────────────────────────────────────────


class TestAtomicRename:
    def test_renames_file_successfully_normal_path(self, tmp_path: Path) -> None:
        src = os.path.join(str(tmp_path), "source.json")
        dest = os.path.join(str(tmp_path), "dest.json")
        Path(src).write_text('{"ok":true}', encoding="utf-8")

        atomic_rename(src, dest)

        assert os.path.exists(dest)
        assert not os.path.exists(src)
        assert Path(dest).read_text(encoding="utf-8") == '{"ok":true}'

    def test_overwrites_existing_destination_file(self, tmp_path: Path) -> None:
        src = os.path.join(str(tmp_path), "new.json")
        dest = os.path.join(str(tmp_path), "old.json")
        Path(dest).write_text('{"version":1}', encoding="utf-8")
        Path(src).write_text('{"version":2}', encoding="utf-8")

        atomic_rename(src, dest)

        assert os.path.exists(dest)
        assert not os.path.exists(src)
        assert Path(dest).read_text(encoding="utf-8") == '{"version":2}'

    def test_reraises_non_permission_errors(self, tmp_path: Path) -> None:
        # Renaming a nonexistent file raises FileNotFoundError (the ENOENT
        # analogue), which is NOT a PermissionError, so it must propagate.
        src = os.path.join(str(tmp_path), "nonexistent.json")
        dest = os.path.join(str(tmp_path), "dest.json")

        with pytest.raises(FileNotFoundError):
            atomic_rename(src, dest)

    def test_persist_round_trip_no_leftover_tmp(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("atomic-test", "1.0.0", ["discovery"], temp_dir)

        assert os.path.exists(os.path.join(temp_dir, "run-state.json"))
        assert not os.path.exists(os.path.join(temp_dir, "run-state.json.tmp"))

        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.run_id == "atomic-test"
        assert (
            loaded.phases["discovery"].status
            == state.phases["discovery"].status
        )

    def test_windows_permission_error_fallback_copies_and_removes_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Added case: the Windows cross-volume ``PermissionError`` fallback.

        Monkeypatch ``os.replace`` (as referenced inside ``run_state``) to raise
        ``PermissionError``; ``atomic_rename`` must fall back to copy+remove,
        writing the destination and deleting the tmp file.
        """
        src = os.path.join(str(tmp_path), "src.json")
        dest = os.path.join(str(tmp_path), "dest.json")
        Path(src).write_text('{"fallback":true}', encoding="utf-8")

        def _raise_permission(_a: str, _b: str) -> None:
            raise PermissionError("simulated EPERM cross-volume")

        # String target avoids importing the module's ``os`` as an attribute
        # (mypy: a module is not an explicit export). Patches the ``os.replace``
        # bound inside run_state for the duration of the test.
        monkeypatch.setattr(
            "pipeline_orchestrator.run_state.os.replace", _raise_permission
        )

        atomic_rename(src, dest)

        assert os.path.exists(dest), "fallback must write the destination file"
        assert not os.path.exists(src), "fallback must remove the tmp/source file"
        assert Path(dest).read_text(encoding="utf-8") == '{"fallback":true}'


# ── validateRunStateIntegrity ────────────────────────────────────────


class TestValidateRunStateIntegrity:
    def test_accepts_valid_run_state_object(self) -> None:
        state = {
            "run_id": "test-run",
            "pipeline_version": "1.0.0",
            "created_at": _now_iso(),
            "updated_at": _now_iso(),
            "status": "initialized",
            "phases": {"discovery": {"phase_name": "discovery", "status": "pending"}},
            "available_artifacts": [],
            "config_path": "/tmp/test",
        }
        assert validate_run_state_integrity(state) is None

    def test_rejects_none(self) -> None:
        err = validate_run_state_integrity(None)
        assert err
        assert "not a valid object" in err

    def test_rejects_non_object_string(self) -> None:
        err = validate_run_state_integrity("not an object")
        assert err
        assert "not a valid object" in err

    def test_rejects_missing_updated_at(self) -> None:
        state = {"run_id": "r1", "status": "initialized", "phases": {"a": {}}}
        err = validate_run_state_integrity(state)
        assert err
        assert "updated_at" in err

    def test_rejects_invalid_iso_date_in_updated_at(self) -> None:
        state = {
            "run_id": "r1",
            "updated_at": "not-a-date",
            "status": "initialized",
            "phases": {"a": {}},
        }
        err = validate_run_state_integrity(state)
        assert err
        assert "not a valid ISO date" in err

    def test_rejects_empty_phases_object(self) -> None:
        state = {
            "run_id": "r1",
            "updated_at": _now_iso(),
            "status": "initialized",
            "phases": {},
        }
        err = validate_run_state_integrity(state)
        assert err
        assert "empty" in err

    def test_rejects_phases_that_is_an_array(self) -> None:
        state = {
            "run_id": "r1",
            "updated_at": _now_iso(),
            "status": "initialized",
            "phases": [],
        }
        err = validate_run_state_integrity(state)
        assert err
        assert "not a valid object" in err

    def test_rejects_missing_run_id(self) -> None:
        state = {
            "updated_at": _now_iso(),
            "status": "initialized",
            "phases": {"a": {}},
        }
        err = validate_run_state_integrity(state)
        assert err
        assert "run_id" in err

    def test_rejects_empty_run_id(self) -> None:
        state = {
            "run_id": "",
            "updated_at": _now_iso(),
            "status": "initialized",
            "phases": {"a": {}},
        }
        err = validate_run_state_integrity(state)
        assert err
        assert "run_id" in err

    def test_rejects_invalid_status_value(self) -> None:
        state = {
            "run_id": "r1",
            "updated_at": _now_iso(),
            "status": "unknown_status",
            "phases": {"a": {}},
        }
        err = validate_run_state_integrity(state)
        assert err
        assert "invalid status" in err

    def test_accepts_all_valid_status_values(self) -> None:
        for status in ["initialized", "running", "completed", "failed"]:
            state = {
                "run_id": "r1",
                "updated_at": _now_iso(),
                "status": status,
                "phases": {"a": {}},
            }
            assert validate_run_state_integrity(state) is None, (
                f'expected status "{status}" to be valid'
            )


# ── loadRunState integrity validation ────────────────────────────────


class TestLoadRunStateIntegrityValidation:
    def test_throws_pipeline_error_for_invalid_json(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        Path(os.path.join(temp_dir, "run-state.json")).write_text(
            "{ invalid json !!!", encoding="utf-8"
        )
        with pytest.raises(PipelineError) as exc_info:
            load_run_state(temp_dir)
        assert exc_info.value.error_class == ErrorClass.file_io_error

    def test_throws_pipeline_error_for_empty_phases(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        corrupt = {
            "run_id": "r1",
            "updated_at": _now_iso(),
            "status": "running",
            "phases": {},
            "available_artifacts": [],
            "config_path": temp_dir,
        }
        Path(os.path.join(temp_dir, "run-state.json")).write_text(
            json.dumps(corrupt), encoding="utf-8"
        )
        with pytest.raises(PipelineError, match="empty"):
            load_run_state(temp_dir)

    def test_throws_pipeline_error_for_invalid_updated_at(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        corrupt = {
            "run_id": "r1",
            "updated_at": "bad-date",
            "status": "running",
            "phases": {"a": {}},
            "available_artifacts": [],
            "config_path": temp_dir,
        }
        Path(os.path.join(temp_dir, "run-state.json")).write_text(
            json.dumps(corrupt), encoding="utf-8"
        )
        with pytest.raises(PipelineError, match="ISO date"):
            load_run_state(temp_dir)

    def test_returns_none_for_unreadable_path(self, tmp_path: Path) -> None:
        result = load_run_state(os.path.join(str(tmp_path), "nonexistent-subdir"))
        assert result is None


# ── recoverRun ───────────────────────────────────────────────────────


class TestRecoverRun:
    def test_recovers_clean_state_no_in_progress(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        init_run("recover-clean", "1.0.0", ["discovery", "curation"], temp_dir)
        result = recover_run(temp_dir)

        assert result.state.run_id == "recover-clean"
        assert result.recovered_phases == []
        assert result.warnings == []

    def test_resets_in_progress_to_pending_and_increments_retry(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run("recover-ip", "1.0.0", ["discovery", "curation"], temp_dir)
        start_phase(state, "discovery", temp_dir)
        result = recover_run(temp_dir)

        assert result.state.phases["discovery"].status == "pending"
        assert result.state.phases["discovery"].retry_count == 1
        assert "discovery" in result.recovered_phases
        assert len(result.warnings) == 1
        assert "discovery" in result.warnings[0]
        assert "retry #1" in result.warnings[0]

    def test_recovers_multiple_in_progress_phases(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run(
            "recover-multi", "1.0.0", ["discovery", "curation", "synthesis"], temp_dir
        )
        state = start_phase(state, "discovery", temp_dir)
        start_phase(state, "curation", temp_dir)
        result = recover_run(temp_dir)

        assert len(result.recovered_phases) == 2
        assert "discovery" in result.recovered_phases
        assert "curation" in result.recovered_phases
        assert result.state.phases["discovery"].status == "pending"
        assert result.state.phases["curation"].status == "pending"

    def test_persists_recovered_state_to_disk(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("recover-persist", "1.0.0", ["discovery"], temp_dir)
        start_phase(state, "discovery", temp_dir)

        recover_run(temp_dir)

        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "pending"
        assert loaded.phases["discovery"].retry_count == 1

    def test_throws_pipeline_error_when_no_state_file(self, tmp_path: Path) -> None:
        empty_dir = os.path.join(str(tmp_path), "recover-empty")
        os.makedirs(empty_dir, exist_ok=True)
        with pytest.raises(PipelineError) as exc_info:
            recover_run(empty_dir)
        assert exc_info.value.error_class == ErrorClass.file_io_error

    def test_corrects_run_status_after_recovering_in_progress(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        state = init_run(
            "recover-status", "1.0.0", ["discovery", "curation"], temp_dir
        )
        start_phase(state, "discovery", temp_dir)
        result = recover_run(temp_dir)

        assert result.state.status == "initialized"

    def test_keeps_running_when_completed_phases_exist(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run(
            "recover-partial", "1.0.0", ["discovery", "curation"], temp_dir
        )
        state = start_phase(state, "discovery", temp_dir)
        state = complete_phase(state, "discovery", temp_dir)
        start_phase(state, "curation", temp_dir)
        result = recover_run(temp_dir)

        assert result.state.phases["curation"].status == "pending"
        assert result.state.status == "running"

    def test_clears_started_at_on_recovered_phases(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("recover-clear", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        assert state.phases["discovery"].started_at

        result = recover_run(temp_dir)
        assert result.state.phases["discovery"].started_at is None


# ── persist on all mutations ─────────────────────────────────────────


class TestPersistOnAllMutations:
    def test_init_run_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        init_run("persist-init", "1.0.0", ["discovery"], temp_dir)
        assert os.path.exists(os.path.join(temp_dir, "run-state.json"))
        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.run_id == "persist-init"

    def test_start_phase_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("persist-start", "1.0.0", ["discovery"], temp_dir)
        start_phase(state, "discovery", temp_dir)
        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "in_progress"

    def test_complete_phase_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("persist-complete", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        complete_phase(state, "discovery", temp_dir)
        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "completed"

    def test_fail_phase_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("persist-fail", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        fail_phase(state, "discovery", "test error", temp_dir)
        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "failed"
        assert loaded.phases["discovery"].error == "test error"

    def test_retry_phase_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("persist-retry", "1.0.0", ["discovery"], temp_dir)
        state = start_phase(state, "discovery", temp_dir)
        state = fail_phase(state, "discovery", "err", temp_dir)
        retry_phase(state, "discovery", temp_dir)
        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "pending"
        assert loaded.phases["discovery"].retry_count == 1

    def test_skip_phase_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("persist-skip", "1.0.0", ["discovery"], temp_dir)
        skip_phase(state, "discovery", temp_dir)
        loaded = load_run_state(temp_dir)
        assert loaded
        assert loaded.phases["discovery"].status == "skipped"

    def test_add_artifact_persists(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("persist-artifact", "1.0.0", ["discovery"], temp_dir)
        add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="test.json",
                phase="discovery",
                created_at=_now_iso(),
            ),
            temp_dir,
        )
        loaded = load_run_state(temp_dir)
        assert loaded
        assert len(loaded.available_artifacts) == 1
        assert loaded.available_artifacts[0].type == "raw-collection"


# ── getCompletedPhases ───────────────────────────────────────────────


class TestGetCompletedPhases:
    def test_returns_empty_when_none_completed(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a", "b", "c"], temp_dir)
        assert get_completed_phases(state) == []

    def test_returns_names_of_completed_phases(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a", "b", "c"], temp_dir)
        state = start_phase(state, "a", temp_dir)
        state = complete_phase(state, "a", temp_dir)
        state = start_phase(state, "b", temp_dir)
        state = complete_phase(state, "b", temp_dir)
        assert get_completed_phases(state) == ["a", "b"]


# ── getSkippedPhases ─────────────────────────────────────────────────


class TestGetSkippedPhases:
    def test_returns_empty_when_none_skipped(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a", "b"], temp_dir)
        assert get_skipped_phases(state) == []

    def test_returns_names_of_skipped_phases(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a", "b", "c"], temp_dir)
        state = skip_phase(state, "b", temp_dir)
        state = skip_phase(state, "c", temp_dir)
        assert get_skipped_phases(state) == ["b", "c"]


# ── getInProgressPhases ──────────────────────────────────────────────


class TestGetInProgressPhases:
    def test_returns_empty_when_none_in_progress(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a", "b"], temp_dir)
        assert get_in_progress_phases(state) == []

    def test_returns_names_of_in_progress_phases(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a", "b", "c"], temp_dir)
        state = start_phase(state, "a", temp_dir)
        assert get_in_progress_phases(state) == ["a"]


# ── getArtifactTypes ─────────────────────────────────────────────────


class TestGetArtifactTypes:
    def test_returns_empty_when_no_artifacts(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a"], temp_dir)
        assert get_artifact_types(state) == []

    def test_returns_all_artifact_types(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a"], temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection", path="a.json", phase="a", created_at=_now_iso()
            ),
            temp_dir,
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="design-spec", path="b.json", phase="a", created_at=_now_iso()
            ),
            temp_dir,
        )
        assert get_artifact_types(state) == ["raw-collection", "design-spec"]

    def test_includes_duplicate_types(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        state = init_run("test-helpers", "1.0.0", ["a"], temp_dir)
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="a1.json",
                phase="a",
                created_at=_now_iso(),
            ),
            temp_dir,
        )
        state = add_artifact(
            state,
            ArtifactRef(
                type="raw-collection",
                path="a2.json",
                phase="a",
                created_at=_now_iso(),
            ),
            temp_dir,
        )
        assert get_artifact_types(state) == ["raw-collection", "raw-collection"]


# ── Feature C step C8 — run-state.json write location ────────────────


class TestFeatureC8WriteLocation:
    def test_writes_run_state_to_run_data_dir_when_params_populated(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        base_dir = os.path.join(temp_dir, "pipeline_mcp_data")
        state_dir = os.path.join(base_dir, "runs", "legacy-runid")

        state = init_run(
            "legacy-runid",
            "v1",
            ["phase-a"],
            state_dir,
            {
                "run_name": "c8-write-test",
                "run_directory_timestamp": "2026-04-10T12:00:00Z",
                "phase_model": "test-model",
            },
        )

        assert state.run_data_dir, "run_data_dir should be defined"
        expected_run_dir = get_run_data_dir(
            base_dir, "c8-write-test", "2026-04-10T12:00:00Z"
        )
        assert state.run_data_dir == expected_run_dir

        sanitized_name = sanitize_run_name("c8-write-test")
        sanitized_ts = sanitize_run_timestamp("2026-04-10T12:00:00Z")
        expected_run_state_path = os.path.join(
            temp_dir,
            "pipeline_mcp_data",
            "runs",
            f"{sanitized_name}-{sanitized_ts}",
            "run-state.json",
        )

        assert os.path.exists(expected_run_state_path)
        assert not os.path.exists(os.path.join(state_dir, "run-state.json"))

        persisted = json.loads(
            Path(expected_run_state_path).read_text(encoding="utf-8")
        )
        assert persisted["run_id"] == "legacy-runid"
        assert persisted["run_data_dir"] == expected_run_dir
        assert persisted["run_parameters"]["run_name"] == "c8-write-test"

    def test_falls_back_to_state_dir_when_params_absent(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        base_dir = os.path.join(temp_dir, "pipeline_mcp_data")
        state_dir = os.path.join(base_dir, "runs", "legacy-runid-nopar")

        state = init_run("legacy-runid-nopar", "v1", ["phase-a"], state_dir)

        assert state.run_data_dir is None
        assert os.path.exists(os.path.join(state_dir, "run-state.json"))

        runs_dir = os.path.join(base_dir, "runs")
        loaded = load_run_state(state_dir)
        assert loaded, "legacy state should load from stateDir"
        assert loaded.run_data_dir is None
        assert os.path.exists(runs_dir)

    def test_subsequent_transitions_land_in_run_data_dir(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        base_dir = os.path.join(temp_dir, "pipeline_mcp_data")
        state_dir = os.path.join(base_dir, "runs", "legacy-runid-trans")

        state = init_run(
            "legacy-runid-trans",
            "v1",
            ["phase-a"],
            state_dir,
            {
                "run_name": "c8-transitions",
                "run_directory_timestamp": "2026-04-10T13:00:00Z",
                "phase_model": "test-model",
            },
        )

        expected_run_dir = get_run_data_dir(
            base_dir, "c8-transitions", "2026-04-10T13:00:00Z"
        )
        run_state_path = os.path.join(expected_run_dir, "run-state.json")
        legacy_state_path = os.path.join(state_dir, "run-state.json")

        assert os.path.exists(run_state_path)
        assert not os.path.exists(legacy_state_path)

        state = start_phase(state, "phase-a", state_dir)
        assert os.path.exists(run_state_path)
        assert not os.path.exists(legacy_state_path)
        persisted = json.loads(Path(run_state_path).read_text(encoding="utf-8"))
        assert persisted["phases"]["phase-a"]["status"] == "in_progress"

        state = complete_phase(state, "phase-a", state_dir)
        assert os.path.exists(run_state_path)
        assert not os.path.exists(legacy_state_path)
        persisted = json.loads(Path(run_state_path).read_text(encoding="utf-8"))
        assert persisted["phases"]["phase-a"]["status"] == "completed"
        assert persisted["status"] == "completed"

    def test_resolve_run_write_dir_returns_run_data_dir_when_set(self) -> None:
        from pipeline_orchestrator.models import RunState

        state = RunState(
            run_id="x",
            pipeline_version="1",
            created_at="t",
            updated_at="t",
            status="initialized",
            config_path="c",
        )
        state.run_data_dir = os.path.join(os.sep, "abs", "path")
        assert resolve_run_write_dir(
            state, os.path.join(os.sep, "fallback")
        ) == os.path.join(os.sep, "abs", "path")

    def test_resolve_run_write_dir_returns_fallback_when_undefined(self) -> None:
        from pipeline_orchestrator.models import RunState

        state = RunState(
            run_id="x",
            pipeline_version="1",
            created_at="t",
            updated_at="t",
            status="initialized",
            config_path="c",
        )
        fallback = os.path.join(os.sep, "fallback")
        assert resolve_run_write_dir(state, fallback) == fallback


def _ms_to_iso(ms: float) -> str:
    """Convert epoch milliseconds to an ISO 8601 ``Z`` string (test helper)."""
    dt = datetime.fromtimestamp(ms / 1000, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"
