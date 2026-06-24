"""Port of ``legacy-node/tests/scaffold-register-outputs.test.ts`` (5 cases).

Byte-exact behavioral parity tests for
:func:`pipeline_orchestrator.tools.artifact_tools.handle_register_scaffold_outputs`:

* registers four scaffold files with correct types + phase + one event per file
* idempotency on a second call (already-registered files skipped, no new events)
* ``PipelineError(artifact_not_found)`` when the scaffold dir is missing
* Feature C: ``run_data_dir/scaffold`` default-dir when ``run_data_dir`` is set
* legacy ``<baseDir>/scaffold`` fallback when ``run_data_dir`` is empty

The Node ``makeCtx`` is reproduced as a :class:`RegisterScaffoldOutputsContext`
backed by real ``run_state`` helpers; ``expose_active_run=True`` makes
``get_active_run`` return the live run (needed by the Feature C default-dir tests).
Fixtures use ``tmp_path``; all paths are OS-agnostic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipeline_orchestrator.errors import PipelineError
from pipeline_orchestrator.models import ArtifactRef, RunState
from pipeline_orchestrator.run_state import add_artifact, init_run
from pipeline_orchestrator.tools.artifact_tools import (
    RegisterScaffoldOutputsContext,
    handle_register_scaffold_outputs,
)


def make_ctx(
    temp_dir: str,
    run_state: list[RunState],
    run_dir: str,
    *,
    expose_active_run: bool = False,
) -> RegisterScaffoldOutputsContext:
    """Build a :class:`RegisterScaffoldOutputsContext` over real run-state helpers.

    By default ``get_active_run`` returns ``None`` so tests hit the legacy
    ``<baseDir>/scaffold`` default-dir branch. Pass ``expose_active_run=True`` to
    have it return the current run state (Feature C ``run_data_dir`` default-dir).
    """

    def _set_active(state: RunState) -> None:
        run_state[0] = state

    def _add_artifact(state: RunState, ref: ArtifactRef, d: str) -> RunState:
        return add_artifact(state, ref, d)

    return RegisterScaffoldOutputsContext(
        require_run=lambda: run_state[0],
        set_active_run=_set_active,
        get_active_run=lambda: (run_state[0] if expose_active_run else None),
        get_active_run_dir=lambda: run_dir,
        base_dir_from_run_dir=lambda _d: temp_dir,
        add_artifact=_add_artifact,
    )


def _read_events(run_dir: str) -> list[dict[str, object]]:
    events_path = os.path.join(run_dir, "events.jsonl")
    if not os.path.exists(events_path):
        return []
    events: list[dict[str, object]] = []
    with open(events_path, encoding="utf-8") as fh:
        content = fh.read()
    for line in content.split("\n"):
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            events.append(json.loads(trimmed))
        except (json.JSONDecodeError, ValueError):
            pass
    return events


class TestHandleRegisterScaffoldOutputs:
    @staticmethod
    def _dirs(tmp_path: Path) -> tuple[str, str, str]:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "scaffold-run")
        scaffold_dir = os.path.join(temp_dir, "scaffold")
        return temp_dir, run_dir, scaffold_dir

    def test_registers_four_scaffold_files_with_correct_types_and_phase(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, scaffold_dir = self._dirs(tmp_path)
        run_state = [
            init_run(
                "scaffold-run-1", "1.0.0", ["implementation_scaffold"], run_dir
            )
        ]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        os.makedirs(scaffold_dir, exist_ok=True)
        with open(
            os.path.join(scaffold_dir, "IMPLEMENTATION-PLAN.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# Implementation Plan\n")
        with open(
            os.path.join(scaffold_dir, "SCAFFOLD-INDEX.md"), "w", encoding="utf-8"
        ) as fh:
            fh.write("# Scaffold Index\n")
        with open(
            os.path.join(scaffold_dir, "AGENTS.md.proposed-diff.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# AGENTS.md proposed diff\n")
        with open(
            os.path.join(scaffold_dir, "scaffold-manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({"version": 1, "files": []}, indent=2))

        result = handle_register_scaffold_outputs({}, ctx)
        assert not result.is_error

        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert len(parsed["data"]["registered"]) == 4
        assert len(parsed["data"]["skipped_existing"]) == 0

        assert len(run_state[0].available_artifacts) == 4
        manifest_refs = [
            a for a in run_state[0].available_artifacts if a.type == "scaffold-manifest"
        ]
        doc_refs = [
            a for a in run_state[0].available_artifacts if a.type == "scaffold-document"
        ]
        assert len(manifest_refs) == 1
        assert len(doc_refs) == 3
        assert manifest_refs[0].path.endswith("scaffold-manifest.json")

        phase_state = run_state[0].phases["implementation_scaffold"]
        assert len(phase_state.output_artifacts) == 4

        manifest_entry = next(
            e
            for e in parsed["data"]["registered"]
            if e["artifact_type"] == "scaffold-manifest"
        )
        assert manifest_entry["version"] == 1
        doc_entries = [
            e
            for e in parsed["data"]["registered"]
            if e["artifact_type"] == "scaffold-document"
        ]
        assert len(doc_entries) == 3
        assert sorted(e["version"] for e in doc_entries) == [1, 2, 3]

        events = _read_events(run_dir)
        stored = [e for e in events if e["event"] == "artifact_stored"]
        assert len(stored) == 4
        for evt in stored:
            assert evt["phase"] == "implementation_scaffold"
            assert evt["run_id"] == "scaffold-run-1"
            details = evt["details"]
            assert isinstance(details, dict)
            assert details["artifact_type"] in (
                "scaffold-manifest",
                "scaffold-document",
            )
            assert isinstance(details["size_bytes"], int)

    def test_is_idempotent_on_second_call_skips_already_registered(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, scaffold_dir = self._dirs(tmp_path)
        run_state = [
            init_run(
                "scaffold-run-2", "1.0.0", ["implementation_scaffold"], run_dir
            )
        ]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        os.makedirs(scaffold_dir, exist_ok=True)
        with open(
            os.path.join(scaffold_dir, "IMPLEMENTATION-PLAN.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# Plan\n")
        with open(
            os.path.join(scaffold_dir, "SCAFFOLD-INDEX.md"), "w", encoding="utf-8"
        ) as fh:
            fh.write("# Index\n")
        with open(
            os.path.join(scaffold_dir, "AGENTS.md.proposed-diff.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# Diff\n")
        with open(
            os.path.join(scaffold_dir, "scaffold-manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({"version": 1}))

        first = handle_register_scaffold_outputs({}, ctx)
        first_parsed = json.loads(first.json)
        assert len(first_parsed["data"]["registered"]) == 4
        assert len(first_parsed["data"]["skipped_existing"]) == 0
        assert len(run_state[0].available_artifacts) == 4

        second = handle_register_scaffold_outputs({}, ctx)
        second_parsed = json.loads(second.json)
        assert len(second_parsed["data"]["registered"]) == 0
        assert len(second_parsed["data"]["skipped_existing"]) == 4
        assert len(run_state[0].available_artifacts) == 4

        phase_state = run_state[0].phases["implementation_scaffold"]
        assert len(phase_state.output_artifacts) == 4

        events = _read_events(run_dir)
        stored = [e for e in events if e["event"] == "artifact_stored"]
        assert len(stored) == 4

    def test_throws_artifact_not_found_when_scaffold_dir_missing(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, _ = self._dirs(tmp_path)
        run_state = [
            init_run(
                "scaffold-run-3", "1.0.0", ["implementation_scaffold"], run_dir
            )
        ]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        missing_path = os.path.join(temp_dir, "does-not-exist", "scaffold")
        with pytest.raises(PipelineError) as exc_info:
            handle_register_scaffold_outputs({"scaffold_dir": missing_path}, ctx)
        assert exc_info.value.error_class == "artifact_not_found"
        assert len(run_state[0].available_artifacts) == 0

    def test_uses_run_data_dir_scaffold_as_default_when_set(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, _ = self._dirs(tmp_path)
        run_data_dir = os.path.join(temp_dir, "runs", "featureC-run-2026-04-10")
        per_run_scaffold_dir = os.path.join(run_data_dir, "scaffold")
        os.makedirs(per_run_scaffold_dir, exist_ok=True)

        initial = init_run(
            "scaffold-run-featureC", "1.0.0", ["implementation_scaffold"], run_dir
        )
        initial.run_data_dir = run_data_dir
        run_state = [initial]

        with open(
            os.path.join(per_run_scaffold_dir, "IMPLEMENTATION-PLAN.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# Plan (per-run)\n")
        with open(
            os.path.join(per_run_scaffold_dir, "SCAFFOLD-INDEX.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# Index (per-run)\n")
        with open(
            os.path.join(per_run_scaffold_dir, "scaffold-manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({"version": 1}))

        ctx = make_ctx(temp_dir, run_state, run_dir, expose_active_run=True)
        result = handle_register_scaffold_outputs({}, ctx)
        assert not result.is_error

        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert parsed["data"]["scaffold_dir"] == per_run_scaffold_dir
        assert len(parsed["data"]["registered"]) == 3
        assert len(parsed["data"]["skipped_existing"]) == 0
        for entry in parsed["data"]["registered"]:
            assert entry["path"].startswith(per_run_scaffold_dir)
        assert len(run_state[0].available_artifacts) == 3
        for ref in run_state[0].available_artifacts:
            assert ref.path.startswith(per_run_scaffold_dir)

    def test_falls_back_to_base_dir_scaffold_when_run_data_dir_empty(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, scaffold_dir = self._dirs(tmp_path)
        run_state = [
            init_run(
                "scaffold-run-legacy",
                "1.0.0",
                ["implementation_scaffold"],
                run_dir,
            )
        ]
        assert run_state[0].run_data_dir is None

        os.makedirs(scaffold_dir, exist_ok=True)
        with open(
            os.path.join(scaffold_dir, "IMPLEMENTATION-PLAN.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write("# Legacy plan\n")
        with open(
            os.path.join(scaffold_dir, "scaffold-manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({"version": 1}))

        ctx = make_ctx(temp_dir, run_state, run_dir, expose_active_run=True)
        result = handle_register_scaffold_outputs({}, ctx)
        assert not result.is_error

        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert parsed["data"]["scaffold_dir"] == scaffold_dir
        assert len(parsed["data"]["registered"]) == 2
        for entry in parsed["data"]["registered"]:
            assert entry["path"].startswith(scaffold_dir)
