"""Port of ``legacy-node/tests/artifact-handlers.test.ts`` (41 cases).

Byte-exact behavioral parity tests for the artifact handlers in
:mod:`pipeline_orchestrator.tools.artifact_tools`. Each Node ``it(...)`` maps 1:1
to a ``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks:

* ``computeNextVersion`` x11 (pure helper)
* ``handleStoreArtifact -- versioning and lineage`` x6
* ``handleListArtifacts -- version and parent_artifact in response`` x1
* ``handleLoadArtifact -- summary-by-default, full, and fields`` x5
* ``handleValidateArtifact -- file_path alternative`` x4
* ``handleStoreArtifact -- emits artifact_stored event`` x2
* ``handleRegisterArtifact -- emits artifact_stored event`` x1
* ``C7 run_data_dir routing`` x11

The Node ``makeCtx`` object literal is reproduced as an :class:`ArtifactContext`
backed by the real ``storage`` + ``run_state`` helpers; the optional
``persistArtifact`` closure (``wirePersistArtifact``) mirrors what ``server.py``
wires. Fixtures use ``tmp_path``; all paths are OS-agnostic (``os.path.join``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipeline_orchestrator.errors import PipelineError
from pipeline_orchestrator.models import ArtifactRef, RunState
from pipeline_orchestrator.run_state import add_artifact, init_run, load_run_state
from pipeline_orchestrator.storage import (
    StorageConfig,
    build_artifact_summary,
    list_artifacts,
    load_artifact,
    persist_artifact,
    storage_key_to_subtype,
    store_artifact,
)
from pipeline_orchestrator.tools.artifact_tools import (
    ArtifactContext,
    compute_next_version,
    handle_list_artifacts,
    handle_load_artifact,
    handle_register_artifact,
    handle_store_artifact,
    handle_validate_artifact,
)
from pipeline_orchestrator.validator import ValidationResult


def _ref(
    type_: str,
    path: str,
    phase: str,
    version: int | None = 1,
) -> ArtifactRef:
    return ArtifactRef(
        type=type_,
        path=path,
        phase=phase,
        created_at="2026-01-01T00:00:00.000Z",
        version=version,
    )


# ── computeNextVersion (pure helper) ──────────────────────────────────


class TestComputeNextVersion:
    def test_returns_1_when_no_existing_artifacts(self) -> None:
        assert compute_next_version([], "raw-collection", "discovery") == 1

    def test_returns_1_when_no_artifacts_match_type_phase(self) -> None:
        artifacts = [_ref("design-spec", "/specs/v1.json", "synthesis", 1)]
        assert compute_next_version(artifacts, "raw-collection", "discovery") == 1

    def test_returns_1_when_type_matches_but_phase_differs(self) -> None:
        artifacts = [_ref("raw-collection", "/raw/v1.json", "curation", 1)]
        assert compute_next_version(artifacts, "raw-collection", "discovery") == 1

    def test_returns_1_when_phase_matches_but_type_differs(self) -> None:
        artifacts = [_ref("design-spec", "/specs/v1.json", "discovery", 1)]
        assert compute_next_version(artifacts, "raw-collection", "discovery") == 1

    def test_returns_2_when_one_existing_artifact_same_type_phase(self) -> None:
        artifacts = [_ref("raw-collection", "/raw/v1.json", "discovery", 1)]
        assert compute_next_version(artifacts, "raw-collection", "discovery") == 2

    def test_returns_max_version_plus_1_when_multiple_versions_exist(self) -> None:
        artifacts = [
            _ref("design-spec", "/specs/v1.json", "synthesis", 1),
            _ref("design-spec", "/specs/v2.json", "synthesis", 2),
            _ref("design-spec", "/specs/v3.json", "synthesis", 3),
        ]
        assert compute_next_version(artifacts, "design-spec", "synthesis") == 4

    def test_handles_non_sequential_version_numbers_picks_true_max(self) -> None:
        artifacts = [
            _ref("design-spec", "/specs/v1.json", "synthesis", 1),
            _ref("design-spec", "/specs/v5.json", "synthesis", 5),
            _ref("design-spec", "/specs/v3.json", "synthesis", 3),
        ]
        assert compute_next_version(artifacts, "design-spec", "synthesis") == 6

    def test_treats_missing_version_field_as_1_via_nullish_coalesce(self) -> None:
        # version is omitted (legacy artifact)
        artifacts = [_ref("raw-collection", "/raw/legacy.json", "discovery", None)]
        assert compute_next_version(artifacts, "raw-collection", "discovery") == 2

    def test_does_not_count_unrelated_phases_toward_the_version(self) -> None:
        artifacts = [
            _ref("design-spec", "/specs/curation-v1.json", "curation", 1),
            _ref("design-spec", "/specs/curation-v2.json", "curation", 2),
            _ref("design-spec", "/specs/synth-v1.json", "synthesis", 1),
        ]
        assert compute_next_version(artifacts, "design-spec", "synthesis") == 2

    def test_each_unique_type_phase_pair_versioned_independently(self) -> None:
        artifacts = [
            _ref("raw-collection", "/raw/v1.json", "discovery", 1),
            _ref("raw-collection", "/raw/v2.json", "discovery", 2),
            _ref("design-spec", "/specs/v1.json", "synthesis", 1),
        ]
        assert compute_next_version(artifacts, "raw-collection", "discovery") == 3
        assert compute_next_version(artifacts, "design-spec", "synthesis") == 2
        assert compute_next_version(artifacts, "design-spec", "discovery") == 1


# ── Mock context factory (mirrors the Node ``makeCtx``) ───────────────


def make_ctx(
    temp_dir: str,
    run_state: list[RunState],
    run_dir: str,
    *,
    wire_persist_artifact: bool = False,
) -> ArtifactContext:
    """Build an :class:`ArtifactContext` over real storage/run-state helpers.

    ``run_state`` is a single-element list used as a mutable cell (the Python
    analogue of the Node ``{ current }`` box) so ``set_active_run`` rebinds.
    """
    sc = StorageConfig(
        base_dir=temp_dir,
        paths={"specs": "specs", "raw_collections": "collections/raw"},
    )
    sc_box: list[StorageConfig] = [sc]

    def _set_active(state: RunState) -> None:
        run_state[0] = state

    def _add_artifact(state: RunState, ref: ArtifactRef, d: str) -> RunState:
        return add_artifact(state, ref, d)

    ctx = ArtifactContext(
        get_schemas=lambda: {},
        validate_artifact=lambda _s, _f, _a: ValidationResult(valid=True, errors=[]),
        store_artifact=lambda config, key, name, artifact, force=None: store_artifact(
            config, key, name, artifact, force
        ),
        load_artifact=lambda config, key, name: load_artifact(config, key, name),
        list_artifacts=lambda config, key: list_artifacts(config, key),
        build_artifact_summary=lambda artifact, key, name: build_artifact_summary(
            artifact, key, name
        ),
        get_storage_config=lambda *_a, **_k: sc_box[0],
        base_dir_from_run_dir=lambda _d: temp_dir,
        get_project_root=lambda: temp_dir,
        get_config_storage_base_dir=lambda: "artifacts",
        get_active_run=lambda: run_state[0],
        set_active_run=_set_active,
        get_active_run_dir=lambda: run_dir,
        require_run=lambda: run_state[0],
        add_artifact=_add_artifact,
    )

    if wire_persist_artifact:
        # Mirror what server.py::artifactCtx does. Looks up the active run's
        # run_data_dir at call time so tests can toggle the field without
        # rebuilding the context.
        def _persist(
            storage_key: str, file_name: str, artifact: object, force: object = None
        ) -> str:
            run_data_dir = run_state[0].run_data_dir
            subtype = storage_key_to_subtype(storage_key)
            if (
                subtype is None
                or not isinstance(run_data_dir, str)
                or len(run_data_dir) == 0
            ):
                raise RuntimeError(
                    f'persistArtifact: cannot route storageKey="{storage_key}" '
                    f'with run_data_dir="{run_data_dir if run_data_dir else ""}" '
                    "to run-scoped path"
                )
            return persist_artifact(
                run_data_dir,
                subtype,
                file_name,
                artifact,
                force if isinstance(force, bool) else None,
            )

        ctx.persist_artifact = _persist

    # Expose the storage-config box so tests can swap it (Node mutates
    # ``ctx.getStorageConfig`` directly).
    ctx._sc_box = sc_box  # type: ignore[attr-defined]
    return ctx


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


# ── handleStoreArtifact — versioning and lineage ──────────────────────


class TestHandleStoreArtifactVersioningAndLineage:
    def test_default_version_1_for_first_artifact_of_type_phase(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-1", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        result = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-v1.json",
                "artifact": {"spec_id": "first"},
                "artifact_type": "design-spec",
                "phase": "discovery",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        assert parsed["data"]["version"] == 1

    def test_version_auto_increments_to_2_when_same_type_phase_stored_twice(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-2", "1.0.0", ["synthesis"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-v1.json",
                "artifact": {"spec_id": "first"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        result = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-v2.json",
                "artifact": {"spec_id": "second"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        assert parsed["data"]["version"] == 2
        assert len(run_state[0].available_artifacts) == 2
        assert run_state[0].available_artifacts[1].version == 2

    def test_different_type_same_phase_each_gets_version_1(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-3", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        r1 = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-first.json",
                "artifact": {"spec_id": "a"},
                "artifact_type": "design-spec",
                "phase": "discovery",
            },
            ctx,
        )
        r2 = handle_store_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "raw-first.json",
                "artifact": {"collection_id": "rc-001"},
                "artifact_type": "raw-collection",
                "phase": "discovery",
            },
            ctx,
        )
        assert json.loads(r1.json)["data"]["version"] == 1
        assert json.loads(r2.json)["data"]["version"] == 1

    def test_same_type_different_phases_each_gets_version_1(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-4", "1.0.0", ["phase-a", "phase-b"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        r1 = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-phase-a.json",
                "artifact": {"spec_id": "a"},
                "artifact_type": "design-spec",
                "phase": "phase-a",
            },
            ctx,
        )
        r2 = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-phase-b.json",
                "artifact": {"spec_id": "b"},
                "artifact_type": "design-spec",
                "phase": "phase-b",
            },
            ctx,
        )
        assert json.loads(r1.json)["data"]["version"] == 1
        assert json.loads(r2.json)["data"]["version"] == 1

    def test_parent_artifact_stored_in_ref_and_response_data(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-5", "1.0.0", ["synthesis"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        result = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "derived-spec.json",
                "artifact": {"spec_id": "derived"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
                "parent_artifact": "/artifacts/raw/original.json",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        assert parsed["data"]["parent_artifact"] == "/artifacts/raw/original.json"
        ref = run_state[0].available_artifacts[0]
        assert ref.parent_artifact == "/artifacts/raw/original.json"

    def test_version_and_parent_preserved_through_store_load_round_trip(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-6", "1.0.0", ["synthesis"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "round-trip.json",
                "artifact": {"spec_id": "rt"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
                "parent_artifact": "raw/source.json",
            },
            ctx,
        )
        reloaded = load_run_state(run_dir)
        assert reloaded is not None
        assert len(reloaded.available_artifacts) == 1
        ref = reloaded.available_artifacts[0]
        assert ref.version == 1
        assert ref.parent_artifact == "raw/source.json"

    def test_rejects_storing_validation_report_under_specs(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "test-run")
        run_state = [init_run("run-conflict-1", "1.0.0", ["validation"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        with pytest.raises(PipelineError) as exc_info:
            handle_store_artifact(
                {
                    "storage_key": "specs",
                    "file_name": "validation-report.json",
                    "artifact": {"report_id": "vr-001", "findings": []},
                    "artifact_type": "validation-report",
                    "phase": "validation",
                },
                ctx,
            )
        err = exc_info.value
        assert err.error_class == "validation_error"
        assert '"validation-report"' in err.message
        assert 'storage_key="specs"' in err.message


# ── handleListArtifacts — version and parent_artifact in response ──────


class TestHandleListArtifactsVersionAndParent:
    def test_list_response_includes_version_and_parent_artifact(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "list-run")
        run_state = [init_run("list-run", "1.0.0", ["synthesis"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-a.json",
                "artifact": {"spec_id": "a"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "spec-b.json",
                "artifact": {"spec_id": "b"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
                "parent_artifact": "specs/spec-a.json",
            },
            ctx,
        )

        list_result = handle_list_artifacts({"storage_key": "specs"}, ctx)
        parsed = json.loads(list_result.json)

        assert parsed["storage_key"] == "specs"
        assert len(parsed["artifacts"]) == 2
        a = next(x for x in parsed["artifacts"] if x["name"] == "spec-a.json")
        b = next(x for x in parsed["artifacts"] if x["name"] == "spec-b.json")
        assert a["version"] == 1
        assert b["version"] == 2
        assert "parent_artifact" not in a
        assert b["parent_artifact"] == "specs/spec-a.json"


# ── handleLoadArtifact — summary-by-default, full, and fields ─────────

_SAMPLE_ARTIFACT = {
    "collection_id": "rc-001",
    "version": "1.0",
    "items": [{"id": "a", "content": "hello"}, {"id": "b", "content": "world"}],
    "tags": ["test"],
    "metadata": {"created_by": "test"},
}


class TestHandleLoadArtifact:
    def test_default_returns_summary_instead_of_full_content(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "load-run")
        run_state = [init_run("load-run-1", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-001.json",
                "artifact": _SAMPLE_ARTIFACT,
                "artifact_type": "raw-collection",
                "phase": "discovery",
            },
            ctx,
        )
        result = handle_load_artifact(
            {"storage_key": "raw_collections", "file_name": "rc-001.json"}, ctx
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert "items" not in parsed
        assert "artifact_type" in parsed
        assert "id" in parsed
        assert "key_count" in parsed

    def test_full_true_returns_complete_artifact_content(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "load-run")
        run_state = [init_run("load-run-2", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-002.json",
                "artifact": _SAMPLE_ARTIFACT,
                "artifact_type": "raw-collection",
                "phase": "discovery",
            },
            ctx,
        )
        result = handle_load_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-002.json",
                "full": True,
            },
            ctx,
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["collection_id"] == "rc-001"
        assert len(parsed["items"]) == 2
        assert parsed["tags"] == ["test"]

    def test_fields_param_returns_only_requested_top_level_keys(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "load-run")
        run_state = [init_run("load-run-3", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-003.json",
                "artifact": _SAMPLE_ARTIFACT,
                "artifact_type": "raw-collection",
                "phase": "discovery",
            },
            ctx,
        )
        result = handle_load_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-003.json",
                "fields": ["collection_id", "tags"],
            },
            ctx,
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["collection_id"] == "rc-001"
        assert parsed["tags"] == ["test"]
        assert "items" not in parsed
        assert "metadata" not in parsed
        assert "version" not in parsed

    def test_fields_param_with_nonexistent_keys_returns_empty_object(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "load-run")
        run_state = [init_run("load-run-4", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-004.json",
                "artifact": _SAMPLE_ARTIFACT,
                "artifact_type": "raw-collection",
                "phase": "discovery",
            },
            ctx,
        )
        result = handle_load_artifact(
            {
                "storage_key": "raw_collections",
                "file_name": "rc-004.json",
                "fields": ["nonexistent_key"],
            },
            ctx,
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed == {}

    def test_throws_artifact_not_found_when_artifact_not_found(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "load-run")
        run_state = [init_run("load-run-5", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        with pytest.raises(PipelineError) as exc_info:
            handle_load_artifact(
                {"storage_key": "raw_collections", "file_name": "missing.json"}, ctx
            )
        err = exc_info.value
        assert err.error_class == "artifact_not_found"
        assert "not found" in err.message


# ── handleValidateArtifact — file_path alternative ────────────────────


class TestHandleValidateArtifactFilePath:
    def test_validates_successfully_when_file_path_points_to_valid_file(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "validate-run")
        run_state = [init_run("validate-run-1", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        artifact_file = os.path.join(temp_dir, "my-artifact.json")
        with open(artifact_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"collection_id": "rc-001", "items": []}))

        result = handle_validate_artifact(
            {"schema": "raw-collection.json", "file_path": artifact_file}, ctx
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert parsed["data"]["valid"] is True

    def test_throws_when_neither_artifact_nor_file_path_provided(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "validate-run")
        run_state = [init_run("validate-run-2", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        with pytest.raises(
            PipelineError,
            match=r"Either artifact \(inline JSON\) or file_path must be provided",
        ):
            handle_validate_artifact({"schema": "raw-collection.json"}, ctx)

    def test_throws_when_file_path_points_to_nonexistent_file(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "validate-run")
        run_state = [init_run("validate-run-3", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        with pytest.raises(PipelineError, match=r"File not found"):
            handle_validate_artifact(
                {
                    "schema": "raw-collection.json",
                    "file_path": os.path.join(temp_dir, "does-not-exist.json"),
                },
                ctx,
            )

    def test_validates_inline_artifact_when_artifact_provided(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "validate-run")
        run_state = [init_run("validate-run-4", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        result = handle_validate_artifact(
            {
                "schema": "raw-collection.json",
                "artifact": {"collection_id": "rc-inline", "items": []},
            },
            ctx,
        )
        assert not result.is_error
        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert parsed["data"]["valid"] is True


# ── handleStoreArtifact — emits artifact_stored event ─────────────────


class TestHandleStoreArtifactEmitsEvent:
    def test_writes_exactly_one_artifact_stored_event_after_store(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "evt-run")
        run_state = [init_run("evt-store-1", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        result = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "evt-spec.json",
                "artifact": {"spec_id": "evt-1"},
                "artifact_type": "design-spec",
                "phase": "discovery",
            },
            ctx,
        )
        stored_path = json.loads(result.json)["data"]["path"]

        events = _read_events(run_dir)
        stored = [e for e in events if e["event"] == "artifact_stored"]
        assert len(stored) == 1
        evt = stored[0]
        assert evt["phase"] == "discovery"
        assert evt["run_id"] == "evt-store-1"
        details = evt["details"]
        assert isinstance(details, dict)
        assert details["artifact_type"] == "design-spec"
        assert details["path"] == stored_path
        assert isinstance(details["size_bytes"], int)
        assert details["size_bytes"] > 0
        assert "content_hash" not in details

    def test_writes_second_event_for_second_store_call(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "evt-run")
        run_state = [init_run("evt-store-2", "1.0.0", ["synthesis"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "v1.json",
                "artifact": {"spec_id": "a"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "v2.json",
                "artifact": {"spec_id": "b"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        events = _read_events(run_dir)
        stored = [e for e in events if e["event"] == "artifact_stored"]
        assert len(stored) == 2


# ── handleRegisterArtifact — emits artifact_stored event ──────────────


class TestHandleRegisterArtifactEmitsEvent:
    def test_writes_one_artifact_stored_event_after_register(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "reg-run")
        run_state = [init_run("evt-reg-1", "1.0.0", ["discovery"], run_dir)]
        ctx = make_ctx(temp_dir, run_state, run_dir)

        external_file = os.path.join(temp_dir, "external-artifact.json")
        with open(external_file, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"collection_id": "ext-1", "items": ["a", "b"]}))

        result = handle_register_artifact(
            {
                "file_path": external_file,
                "artifact_type": "raw-collection",
                "phase": "discovery",
                "storage_key": "raw_collections",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        assert parsed["registered"] is True

        events = _read_events(run_dir)
        stored = [e for e in events if e["event"] == "artifact_stored"]
        assert len(stored) == 1
        evt = stored[0]
        assert evt["phase"] == "discovery"
        assert evt["run_id"] == "evt-reg-1"
        details = evt["details"]
        assert isinstance(details, dict)
        assert details["artifact_type"] == "raw-collection"
        assert details["path"] == parsed["path"]
        assert isinstance(details["size_bytes"], int)
        assert details["size_bytes"] > 0


# ── C7 run_data_dir routing (Feature C) ───────────────────────────────


class TestC7RunDataDirRouting:
    @staticmethod
    def _dirs(tmp_path: Path) -> tuple[str, str, str]:
        temp_dir = str(tmp_path)
        run_dir = os.path.join(temp_dir, "runs", "c7-run")
        run_data_dir = os.path.join(
            temp_dir, "runs", "c7-run-2026-04-10T09-13-58Z"
        )
        return temp_dir, run_dir, run_data_dir

    def test_store_writes_under_run_data_dir_specs_when_set(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-store-1", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        result = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "c7-spec.json",
                "artifact": {"spec_id": "c7-1"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        expected = os.path.join(run_data_dir, "specs", "c7-spec.json")
        assert parsed["data"]["path"] == expected
        assert os.path.exists(expected)
        assert not os.path.exists(os.path.join(temp_dir, "specs", "c7-spec.json"))

    def test_store_writes_under_legacy_base_dir_specs_when_run_data_dir_empty(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, _ = self._dirs(tmp_path)
        initial = init_run("c7-store-2", "1.0.0", ["synthesis"], run_dir)
        # Intentionally leave run_data_dir unset.
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        result = handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "legacy-spec.json",
                "artifact": {"spec_id": "legacy-1"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        expected = os.path.join(temp_dir, "specs", "legacy-spec.json")
        assert parsed["data"]["path"] == expected
        assert os.path.exists(expected)

    def test_store_throws_expected_collision_error_run_scoped(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-store-3", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        handle_store_artifact(
            {
                "storage_key": "specs",
                "file_name": "collide.json",
                "artifact": {"id": "v1"},
                "artifact_type": "design-spec",
                "phase": "synthesis",
            },
            ctx,
        )
        expected_path = os.path.join(run_data_dir, "specs", "collide.json")
        with pytest.raises(FileExistsError) as exc_info:
            handle_store_artifact(
                {
                    "storage_key": "specs",
                    "file_name": "collide.json",
                    "artifact": {"id": "v2"},
                    "artifact_type": "design-spec",
                    "phase": "synthesis",
                },
                ctx,
            )
        assert str(exc_info.value) == (
            f'Artifact already exists at "{expected_path}". '
            "Pass force=true to overwrite."
        )
        with open(expected_path, encoding="utf-8") as fh:
            existing = json.load(fh)
        assert existing["id"] == "v1"

    def test_store_unknown_storage_key_falls_through_to_legacy(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-store-4", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        # Inject an extra StorageConfig path so store_artifact can resolve it.
        ctx._sc_box[0] = StorageConfig(  # type: ignore[attr-defined]
            base_dir=temp_dir,
            paths={
                "specs": "specs",
                "raw_collections": "collections/raw",
                "manifests": "manifests",
            },
        )

        result = handle_store_artifact(
            {
                "storage_key": "manifests",
                "file_name": "run-manifest.json",
                "artifact": {"manifest_id": "mf-1"},
                "artifact_type": "run-manifest",
                "phase": "synthesis",
            },
            ctx,
        )
        parsed = json.loads(result.json)
        expected = os.path.join(temp_dir, "manifests", "run-manifest.json")
        assert parsed["data"]["path"] == expected
        assert os.path.exists(expected)
        assert not os.path.exists(
            os.path.join(run_data_dir, "manifests", "run-manifest.json")
        )

    def test_load_reads_from_run_data_dir_first_when_set(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-load-1", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        os.makedirs(os.path.join(run_data_dir, "specs"), exist_ok=True)
        with open(
            os.path.join(run_data_dir, "specs", "run-scoped.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({"source": "run-scoped", "key": "value"}, indent=2))

        result = handle_load_artifact(
            {
                "storage_key": "specs",
                "file_name": "run-scoped.json",
                "full": True,
            },
            ctx,
        )
        parsed = json.loads(result.json)
        assert parsed["source"] == "run-scoped"

    def test_load_falls_back_to_legacy_when_run_scoped_missing(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-load-2", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        os.makedirs(os.path.join(temp_dir, "specs"), exist_ok=True)
        with open(
            os.path.join(temp_dir, "specs", "legacy-only.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({"source": "legacy"}, indent=2))

        result = handle_load_artifact(
            {
                "storage_key": "specs",
                "file_name": "legacy-only.json",
                "full": True,
            },
            ctx,
        )
        parsed = json.loads(result.json)
        assert parsed["source"] == "legacy"

    def test_load_reads_legacy_path_when_run_data_dir_absent(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, _ = self._dirs(tmp_path)
        initial = init_run("c7-load-3", "1.0.0", ["synthesis"], run_dir)
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        os.makedirs(os.path.join(temp_dir, "specs"), exist_ok=True)
        with open(
            os.path.join(temp_dir, "specs", "no-rdd.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({"source": "legacy-only"}, indent=2))

        result = handle_load_artifact(
            {"storage_key": "specs", "file_name": "no-rdd.json", "full": True}, ctx
        )
        parsed = json.loads(result.json)
        assert parsed["source"] == "legacy-only"

    def test_list_lists_from_run_data_dir_when_set(self, tmp_path: Path) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-list-1", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        os.makedirs(os.path.join(run_data_dir, "specs"), exist_ok=True)
        with open(
            os.path.join(run_data_dir, "specs", "a.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({"id": "a"}))
        with open(
            os.path.join(run_data_dir, "specs", "b.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({"id": "b"}))
        os.makedirs(os.path.join(temp_dir, "specs"), exist_ok=True)
        with open(
            os.path.join(temp_dir, "specs", "legacy-only.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(json.dumps({}))

        result = handle_list_artifacts({"storage_key": "specs"}, ctx)
        parsed = json.loads(result.json)
        assert parsed["storage_key"] == "specs"
        names = sorted(a["name"] for a in parsed["artifacts"])
        assert names == ["a.json", "b.json"]
        assert "legacy-only.json" not in names

    def test_list_returns_empty_when_run_scoped_dir_not_yet_exist(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-list-2", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        result = handle_list_artifacts({"storage_key": "specs"}, ctx)
        parsed = json.loads(result.json)
        assert parsed["storage_key"] == "specs"
        assert parsed["artifacts"] == []

    def test_list_falls_back_to_legacy_when_run_data_dir_absent(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, _ = self._dirs(tmp_path)
        initial = init_run("c7-list-3", "1.0.0", ["synthesis"], run_dir)
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        os.makedirs(os.path.join(temp_dir, "specs"), exist_ok=True)
        with open(
            os.path.join(temp_dir, "specs", "l1.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({}))
        with open(
            os.path.join(temp_dir, "specs", "l2.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({}))

        result = handle_list_artifacts({"storage_key": "specs"}, ctx)
        parsed = json.loads(result.json)
        names = sorted(a["name"] for a in parsed["artifacts"])
        assert names == ["l1.json", "l2.json"]

    def test_list_unknown_storage_key_falls_through_to_legacy(
        self, tmp_path: Path
    ) -> None:
        temp_dir, run_dir, run_data_dir = self._dirs(tmp_path)
        initial = init_run("c7-list-4", "1.0.0", ["synthesis"], run_dir)
        initial.run_data_dir = run_data_dir
        run_state = [initial]
        ctx = make_ctx(temp_dir, run_state, run_dir, wire_persist_artifact=True)

        ctx._sc_box[0] = StorageConfig(  # type: ignore[attr-defined]
            base_dir=temp_dir,
            paths={
                "specs": "specs",
                "raw_collections": "collections/raw",
                "manifests": "manifests",
            },
        )
        os.makedirs(os.path.join(temp_dir, "manifests"), exist_ok=True)
        with open(
            os.path.join(temp_dir, "manifests", "m1.json"), "w", encoding="utf-8"
        ) as fh:
            fh.write(json.dumps({}))

        result = handle_list_artifacts({"storage_key": "manifests"}, ctx)
        parsed = json.loads(result.json)
        names = [a["name"] for a in parsed["artifacts"]]
        assert names == ["m1.json"]
