"""Port of ``legacy-node/tests/storage.test.ts`` (58 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.storage``. Paths are
built OS-agnostically with ``os.path.join`` and rooted at the pytest ``tmp_path``
fixture (never hardcoded POSIX strings), so the same assertions pass on Windows
and POSIX. JSON round-trips via ``json.loads``; no test asserts raw file bytes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipeline_orchestrator.storage import (
    StorageConfig,
    build_artifact_summary,
    get_artifact_dir,
    get_run_data_dir,
    list_artifacts,
    load_artifact,
    persist_artifact,
    sanitize_run_name,
    sanitize_run_timestamp,
    storage_key_to_subtype,
    store_artifact,
)


@pytest.fixture
def storage_config(tmp_path: Path) -> StorageConfig:
    """A ``StorageConfig`` rooted at a fresh ``tmp_path`` (mirrors beforeEach)."""
    return StorageConfig(
        base_dir=str(tmp_path),
        paths={
            "raw_collections": os.path.join("collections", "raw"),
            "curated_collections": os.path.join("collections", "curated"),
            "specs": "specs",
            "overviews": "overviews",
            "debates": "debates",
        },
    )


# ── storeArtifact ────────────────────────────────────────────────────


def test_store_stores_json_artifact_in_correct_directory(
    storage_config: StorageConfig,
) -> None:
    artifact = {"collection_id": "test-123", "status": "raw", "items": []}
    path = store_artifact(storage_config, "raw_collections", "test-123.json", artifact)

    assert os.path.exists(path)
    stored = json.loads(Path(path).read_text(encoding="utf-8"))
    assert stored["collection_id"] == "test-123"


def test_store_creates_intermediate_directories(
    storage_config: StorageConfig,
) -> None:
    artifact = {"spec_id": "abc", "title": "Test Spec"}
    path = store_artifact(storage_config, "specs", "test-spec.json", artifact)

    assert os.path.exists(path)
    assert "specs" in path


def test_store_throws_on_unknown_storage_key(
    storage_config: StorageConfig,
) -> None:
    with pytest.raises(ValueError, match="(?i)unknown storage key"):
        store_artifact(storage_config, "nonexistent", "file.json", {})


def test_store_throws_when_overwriting_without_force(
    storage_config: StorageConfig,
) -> None:
    store_artifact(storage_config, "specs", "existing.json", {"id": "v1"})
    with pytest.raises(
        FileExistsError, match=r"Artifact already exists.*force=true"
    ):
        store_artifact(storage_config, "specs", "existing.json", {"id": "v2"})


def test_store_allows_overwrite_with_force_true(
    storage_config: StorageConfig,
) -> None:
    store_artifact(storage_config, "specs", "existing.json", {"id": "v1"})
    path = store_artifact(storage_config, "specs", "existing.json", {"id": "v2"}, True)
    loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    assert loaded["id"] == "v2"


# ── loadArtifact ─────────────────────────────────────────────────────


def test_load_loads_previously_stored_artifact(
    storage_config: StorageConfig,
) -> None:
    artifact = {"id": "test", "data": "hello"}
    store_artifact(storage_config, "specs", "test.json", artifact)

    loaded = load_artifact(storage_config, "specs", "test.json")
    assert loaded == artifact


def test_load_returns_none_for_missing_artifact(
    storage_config: StorageConfig,
) -> None:
    loaded = load_artifact(storage_config, "specs", "nonexistent.json")
    assert loaded is None


# ── listArtifacts ────────────────────────────────────────────────────


def test_list_lists_all_artifacts_in_a_storage_path(
    storage_config: StorageConfig,
) -> None:
    store_artifact(storage_config, "specs", "a.json", {"id": "a"})
    store_artifact(storage_config, "specs", "b.json", {"id": "b"})

    files = list_artifacts(storage_config, "specs")
    assert len(files) == 2
    assert "a.json" in files
    assert "b.json" in files


def test_list_returns_empty_array_for_nonexistent_directory(
    storage_config: StorageConfig,
) -> None:
    files = list_artifacts(storage_config, "overviews")
    assert files == []


# ── buildArtifactSummary ─────────────────────────────────────────────


def test_summary_artifact_type_and_id() -> None:
    summary = build_artifact_summary({"a": 1}, "specs", "spec-abc.json")
    assert summary.artifact_type == "specs"
    assert summary.id == "spec-abc.json"


def test_summary_reports_correct_key_count_for_small_object() -> None:
    artifact = {"spec_id": "x", "title": "y", "version": "1.0.0"}
    summary = build_artifact_summary(artifact, "specs", "test.json")
    assert summary.key_count == 3
    assert summary.truncated is False
    assert summary.preview_keys == ["spec_id", "title", "version"]


def test_summary_reports_total_size_bytes_as_json_byte_length() -> None:
    artifact = {"key": "value"}
    summary = build_artifact_summary(artifact, "specs", "size.json")
    compact = json.dumps(artifact, separators=(",", ":"))
    assert summary.total_size_bytes == len(compact.encode("utf-8"))
    assert summary.total_size_bytes == 15  # {"key":"value"} = 15 bytes


def test_summary_truncates_preview_to_20_keys_for_large_object() -> None:
    artifact = {f"key_{i:02d}": i for i in range(25)}
    summary = build_artifact_summary(artifact, "raw_collections", "large.json")
    assert summary.key_count == 25
    assert summary.truncated is True
    assert len(summary.preview_keys) == 20
    assert summary.preview_keys[0] == "key_00"
    assert summary.preview_keys[19] == "key_19"


def test_summary_truncated_false_when_key_count_equals_limit() -> None:
    artifact = {f"k{i}": i for i in range(20)}
    summary = build_artifact_summary(artifact, "specs", "boundary.json")
    assert summary.key_count == 20
    assert summary.truncated is False
    assert len(summary.preview_keys) == 20


def test_summary_empty_for_non_object_artifacts() -> None:
    summary = build_artifact_summary("a string", "specs", "scalar.json")
    assert summary.key_count == 0
    assert summary.truncated is False
    assert summary.preview_keys == []


def test_summary_empty_for_null_artifact() -> None:
    summary = build_artifact_summary(None, "specs", "null.json")
    assert summary.key_count == 0
    assert summary.truncated is False
    assert summary.preview_keys == []


def test_summary_array_keys_are_string_indices() -> None:
    # Arrays are objects in JS, so Object.keys returns '0', '1', '2'.
    summary = build_artifact_summary([1, 2, 3], "specs", "array.json")
    assert summary.key_count == 3
    assert summary.preview_keys == ["0", "1", "2"]


# ── Per-Run Hierarchical Artifact Directory Helpers (Feature C) ──────

# ── sanitizeRunName ──────────────────────────────────────────────────


def test_sanitize_name_passes_through_ascii_lowercased() -> None:
    assert sanitize_run_name("abc123") == "abc123"
    assert sanitize_run_name("ABC123") == "abc123"


def test_sanitize_name_preserves_single_hyphens() -> None:
    assert sanitize_run_name("fix-quality-gate") == "fix-quality-gate"


def test_sanitize_name_replaces_punctuation_and_collapses_runs() -> None:
    assert sanitize_run_name("Fix Quality Gate") == "fix-quality-gate"
    assert sanitize_run_name("fix:quality_gate.run") == "fix-quality-gate-run"


def test_sanitize_name_strips_windows_unsafe_characters() -> None:
    sanitized = sanitize_run_name('a\\b/c:d*e?f"g<h>i|j')
    assert sanitized == "a-b-c-d-e-f-g-h-i-j"
    for ch in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        assert ch not in sanitized, f"unsafe char {ch} should be stripped"


def test_sanitize_name_replaces_non_ascii_unicode() -> None:
    assert sanitize_run_name("тест-фикс") == "unnamed-run"
    assert sanitize_run_name("café-fix") == "caf-fix"
    assert sanitize_run_name("修复-bug") == "bug"
    assert sanitize_run_name("rocket-🚀-launch") == "rocket-launch"


def test_sanitize_name_trims_leading_and_trailing_hyphens() -> None:
    assert sanitize_run_name("  --fix--bug--  ") == "fix-bug"
    assert sanitize_run_name("---") == "unnamed-run"


def test_sanitize_name_truncates_to_64_characters() -> None:
    long = "a" * 200
    sanitized = sanitize_run_name(long)
    assert len(sanitized) == 64
    assert all(c == "a" for c in sanitized)


def test_sanitize_name_truncation_does_not_leave_trailing_hyphen() -> None:
    # 63 chars of 'a' then a space (-> '-') then 10 'b' chars; the slice lands
    # at the 64-char boundary on a hyphen which must then be trimmed.
    input_name = "a" * 63 + " " + "b" * 10
    sanitized = sanitize_run_name(input_name)
    assert len(sanitized) == 63, "trailing hyphen at slice boundary should be trimmed"
    assert not sanitized.endswith("-")


def test_sanitize_name_unnamed_run_for_empty_whitespace_nonstring() -> None:
    assert sanitize_run_name("") == "unnamed-run"
    assert sanitize_run_name("   ") == "unnamed-run"
    assert sanitize_run_name("!!!") == "unnamed-run"
    # Defensive — runtime callers may pass None despite the type signature.
    assert sanitize_run_name(None) == "unnamed-run"  # type: ignore[arg-type]


# ── sanitizeRunTimestamp ─────────────────────────────────────────────


def test_sanitize_timestamp_replaces_colons_and_dots() -> None:
    assert sanitize_run_timestamp("2026-04-10T09:13:58Z") == "2026-04-10T09-13-58Z"
    assert (
        sanitize_run_timestamp("2026-04-10T09:13:58.123Z")
        == "2026-04-10T09-13-58-123Z"
    )


def test_sanitize_timestamp_passes_through_date_and_tz_marker() -> None:
    result = sanitize_run_timestamp("2026-04-10T09:13:58Z")
    assert result.startswith("2026-04-10T")
    assert result.endswith("Z")


def test_sanitize_timestamp_contains_no_windows_unsafe_characters() -> None:
    result = sanitize_run_timestamp("2026-04-10T09:13:58.123Z")
    for ch in [":", ".", "/", "\\", "*", "?", '"', "<", ">", "|"]:
        assert ch not in result, f"unsafe char {ch} should be stripped"


def test_sanitize_timestamp_returns_empty_for_empty_or_nonstring() -> None:
    assert sanitize_run_timestamp("") == ""
    assert sanitize_run_timestamp(None) == ""  # type: ignore[arg-type]


# ── getRunDataDir ────────────────────────────────────────────────────


def test_run_data_dir_joins_base_runs_and_name_timestamp() -> None:
    directory = get_run_data_dir(
        "pipeline_mcp_data", "fix-quality-gate", "2026-04-10T09:13:58Z"
    )
    assert directory == os.path.join(
        "pipeline_mcp_data", "runs", "fix-quality-gate-2026-04-10T09-13-58Z"
    )


def test_run_data_dir_always_sits_under_base_runs() -> None:
    directory = get_run_data_dir(
        "pipeline_mcp_data", "My Run", "2026-04-10T09:13:58Z"
    )
    assert os.path.join("pipeline_mcp_data", "runs") in directory


def test_run_data_dir_sanitizes_run_name_component() -> None:
    directory = get_run_data_dir(
        "base", "Fix:Quality/Gate*Mismatch", "2026-04-10T09:13:58Z"
    )
    assert directory.endswith(
        os.path.join("runs", "fix-quality-gate-mismatch-2026-04-10T09-13-58Z")
    )


def test_run_data_dir_omits_timestamp_suffix_when_empty() -> None:
    directory = get_run_data_dir("base", "fix-bug", "")
    assert directory == os.path.join("base", "runs", "fix-bug")


def test_run_data_dir_falls_back_to_unnamed_run() -> None:
    directory = get_run_data_dir("base", "!!!", "2026-04-10T09:13:58Z")
    assert directory == os.path.join(
        "base", "runs", "unnamed-run-2026-04-10T09-13-58Z"
    )


def test_run_data_dir_produces_no_windows_unsafe_characters() -> None:
    directory = get_run_data_dir("base", "a/b\\c:d", "2026-04-10T09:13:58.123Z")
    # Final dir component only; the join separators are platform-native.
    dir_name = os.path.basename(directory)
    for ch in [":", ".", "*", "?", '"', "<", ">", "|"]:
        assert ch not in dir_name, f"unsafe char {ch} should not appear in dir name"


# ── getArtifactDir ───────────────────────────────────────────────────

_RUN_DATA_DIR = os.path.join(
    "pipeline_mcp_data", "runs", "my-run-2026-04-10T09-13-58Z"
)


def test_artifact_dir_curated_collections_subtype() -> None:
    assert get_artifact_dir(_RUN_DATA_DIR, "collections/curated") == os.path.join(
        _RUN_DATA_DIR, "collections", "curated"
    )


def test_artifact_dir_raw_collections_subtype() -> None:
    assert get_artifact_dir(_RUN_DATA_DIR, "collections/raw") == os.path.join(
        _RUN_DATA_DIR, "collections", "raw"
    )


def test_artifact_dir_debates_subtype() -> None:
    assert get_artifact_dir(_RUN_DATA_DIR, "debates") == os.path.join(
        _RUN_DATA_DIR, "debates"
    )


def test_artifact_dir_overviews_subtype() -> None:
    assert get_artifact_dir(_RUN_DATA_DIR, "overviews") == os.path.join(
        _RUN_DATA_DIR, "overviews"
    )


def test_artifact_dir_specs_subtype() -> None:
    assert get_artifact_dir(_RUN_DATA_DIR, "specs") == os.path.join(
        _RUN_DATA_DIR, "specs"
    )


def test_artifact_dir_scaffold_subtype() -> None:
    assert get_artifact_dir(_RUN_DATA_DIR, "scaffold") == os.path.join(
        _RUN_DATA_DIR, "scaffold"
    )


def test_artifact_dir_uses_platform_native_separators_on_prefix() -> None:
    directory = get_artifact_dir(_RUN_DATA_DIR, "overviews")
    assert directory.startswith(_RUN_DATA_DIR)


# ── storageKeyToSubtype ──────────────────────────────────────────────


def test_subtype_maps_raw_collections() -> None:
    assert storage_key_to_subtype("raw_collections") == "collections/raw"


def test_subtype_maps_curated_collections() -> None:
    assert storage_key_to_subtype("curated_collections") == "collections/curated"


def test_subtype_maps_overviews() -> None:
    assert storage_key_to_subtype("overviews") == "overviews"


def test_subtype_maps_specs() -> None:
    assert storage_key_to_subtype("specs") == "specs"


def test_subtype_maps_debates() -> None:
    assert storage_key_to_subtype("debates") == "debates"


def test_subtype_maps_scaffold() -> None:
    assert storage_key_to_subtype("scaffold") == "scaffold"


def test_subtype_returns_none_for_unknown_keys() -> None:
    assert storage_key_to_subtype("manifests") is None
    assert storage_key_to_subtype("codebase") is None
    assert storage_key_to_subtype("runs") is None


def test_subtype_returns_none_for_empty_string() -> None:
    assert storage_key_to_subtype("") is None


# ── persistArtifact ──────────────────────────────────────────────────


@pytest.fixture
def run_data_dir(tmp_path: Path) -> str:
    """A simulated per-run data dir nested under ``tmp_path`` (mirrors beforeEach)."""
    return os.path.join(str(tmp_path), "runs", "my-run-2026-04-10T09-13-58Z")


def test_persist_writes_under_run_dir_subtype_filename(
    run_data_dir: str, tmp_path: Path
) -> None:
    artifact = {"spec_id": "abc", "title": "Test Spec"}
    file_path = persist_artifact(run_data_dir, "specs", "test-spec.json", artifact)

    assert os.path.exists(file_path), "file should exist on disk"
    assert file_path == os.path.join(run_data_dir, "specs", "test-spec.json")
    # Path should be absolute (nested under tmp_path which is absolute).
    assert file_path.startswith(str(tmp_path))

    stored = json.loads(Path(file_path).read_text(encoding="utf-8"))
    assert stored == artifact


def test_persist_recursively_creates_nested_subtype_dirs(
    run_data_dir: str,
) -> None:
    artifact = {"collection_id": "rc-1", "items": []}
    file_path = persist_artifact(run_data_dir, "collections/raw", "rc-1.json", artifact)

    assert os.path.exists(file_path)
    assert file_path == os.path.join(run_data_dir, "collections", "raw", "rc-1.json")


def test_persist_throws_on_collision_default_and_preserves_content(
    run_data_dir: str,
) -> None:
    persist_artifact(run_data_dir, "specs", "collide.json", {"id": "v1"})
    with pytest.raises(
        FileExistsError, match=r"Artifact already exists.*force=true"
    ):
        persist_artifact(run_data_dir, "specs", "collide.json", {"id": "v2"})
    # Existing file untouched.
    existing = json.loads(
        Path(os.path.join(run_data_dir, "specs", "collide.json")).read_text(
            encoding="utf-8"
        )
    )
    assert existing["id"] == "v1"


def test_persist_throws_on_collision_when_force_false(
    run_data_dir: str,
) -> None:
    persist_artifact(run_data_dir, "debates", "d.json", {"id": "first"})
    with pytest.raises(
        FileExistsError, match=r"Artifact already exists.*force=true"
    ):
        persist_artifact(run_data_dir, "debates", "d.json", {"id": "second"}, False)


def test_persist_overwrites_when_force_true(
    run_data_dir: str,
) -> None:
    persist_artifact(run_data_dir, "specs", "over.json", {"id": "v1"})
    file_path = persist_artifact(run_data_dir, "specs", "over.json", {"id": "v2"}, True)
    loaded = json.loads(Path(file_path).read_text(encoding="utf-8"))
    assert loaded["id"] == "v2"


def test_persist_error_message_matches_store_byte_for_byte(
    run_data_dir: str,
) -> None:
    persist_artifact(run_data_dir, "specs", "msg.json", {"id": 1})
    expected = os.path.join(run_data_dir, "specs", "msg.json")
    with pytest.raises(FileExistsError) as exc_info:
        persist_artifact(run_data_dir, "specs", "msg.json", {"id": 2})
    assert str(exc_info.value) == (
        f'Artifact already exists at "{expected}". Pass force=true to overwrite.'
    )


def test_persist_round_trips_arbitrary_json_payloads(
    run_data_dir: str,
) -> None:
    artifact = {
        "scalar": 42,
        "nested": {"a": [1, 2, 3], "b": {"c": "hello"}},
        "list": ["x", "y"],
        "flag": True,
        "nullField": None,
    }
    file_path = persist_artifact(
        run_data_dir, "overviews", "round-trip.json", artifact
    )
    loaded = json.loads(Path(file_path).read_text(encoding="utf-8"))
    assert loaded == artifact
