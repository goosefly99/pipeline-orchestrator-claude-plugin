"""Port of ``legacy-node/tests/per-run-paths.test.ts`` (12 cases).

Feature C integration tests for the per-run hierarchical artifact directory
structure, covering three scopes:

  (a) Unit tests on ``sanitize_run_name`` for Windows-unsafe + Unicode inputs.
  (b) A two-run sequence asserting zero path collisions when both runs use
      identical file names under their own per-run trees.
  (c) Legacy fallback: a pre-Feature-C run (no ``run_data_dir``) still resolves
      artifacts from the legacy top-level directories.

In the Node tree scopes (b)/(c) drive the ``initRun`` (``run-state.ts``) and
``handleStoreArtifact``/``handleListArtifacts`` (``artifact-handlers.ts``)
layers, which are ported in later tasks. Here those layers are reproduced as
small test-local helpers built **only** on ``pipeline_orchestrator.storage``
primitives, faithfully mirroring the storage-level routing the Node handlers
perform: ``initRun`` populates ``run_data_dir`` via ``get_run_data_dir`` only
when both ``run_name`` and ``run_directory_timestamp`` are present; the store
path routes to ``persist_artifact`` when ``run_data_dir`` is set and the
``storage_key`` maps to a subtype, else falls back to ``store_artifact``; the
list path reads the run-scoped ``get_artifact_dir`` when routable, else falls
back to ``list_artifacts``. Paths are OS-agnostic (``os.path.join``) and rooted
at the ``tmp_path`` fixture.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline_orchestrator.storage import (
    StorageConfig,
    get_artifact_dir,
    get_run_data_dir,
    list_artifacts,
    persist_artifact,
    sanitize_run_name,
    sanitize_run_timestamp,
    storage_key_to_subtype,
    store_artifact,
)

# ── Test-local re-creations of the run-state / handler routing ───────
#
# These mirror, at the storage layer only, the exact decisions the Node
# ``initRun`` + handler code makes. They live in the test (not in storage.py)
# because the full run-state / handler ports belong to later tasks; T1.1 owns
# only the storage primitives they compose.

_STORAGE_PATHS = {
    "specs": "specs",
    "overviews": "overviews",
    "debates": "debates",
    "raw_collections": os.path.join("collections", "raw"),
    "curated_collections": os.path.join("collections", "curated"),
    "scaffold": "scaffold",
}


def _init_run_data_dir(
    state_dir: str,
    run_name: str | None = None,
    timestamp: str | None = None,
) -> str | None:
    """Mirror ``initRun``'s ``run_data_dir`` computation (run-state.ts).

    Returns the per-run data dir only when BOTH ``run_name`` and ``timestamp``
    are present; otherwise ``None`` (legacy pre-Feature-C run). The Node base
    dir is ``dirname(dirname(stateDir))``.
    """
    if run_name and timestamp:
        base_dir = os.path.dirname(os.path.dirname(state_dir))
        return get_run_data_dir(base_dir, run_name, timestamp)
    return None


def _store(
    config: StorageConfig,
    run_data_dir: str | None,
    storage_key: str,
    file_name: str,
    artifact: object,
    force: bool | None = None,
) -> str:
    """Mirror ``handleStoreArtifact`` routing: run-scoped persist vs legacy store."""
    subtype = storage_key_to_subtype(storage_key)
    can_use_run_scoped = (
        isinstance(run_data_dir, str) and len(run_data_dir) > 0 and subtype is not None
    )
    if can_use_run_scoped:
        assert run_data_dir is not None and subtype is not None
        return persist_artifact(run_data_dir, subtype, file_name, artifact, force)
    return store_artifact(config, storage_key, file_name, artifact, force)


def _list(
    config: StorageConfig,
    run_data_dir: str | None,
    storage_key: str,
) -> list[str]:
    """Mirror ``handleListArtifacts`` routing: run-scoped list vs legacy list."""
    subtype = storage_key_to_subtype(storage_key)
    use_run_scoped = (
        isinstance(run_data_dir, str) and len(run_data_dir) > 0 and subtype is not None
    )
    if use_run_scoped:
        assert run_data_dir is not None and subtype is not None
        run_scoped_dir = get_artifact_dir(run_data_dir, subtype)
        if os.path.exists(run_scoped_dir):
            return [f for f in os.listdir(run_scoped_dir) if f.endswith(".json")]
        return []
    return list_artifacts(config, storage_key)


def _make_config(base_dir: str) -> StorageConfig:
    return StorageConfig(base_dir=base_dir, paths=dict(_STORAGE_PATHS))


# ── (a) sanitizeRunName: Windows-unsafe + Unicode ───────────────────


def test_a_strips_every_windows_reserved_filename_character() -> None:
    input_name = 'a\\b/c:d*e?f"g<h>i|j'
    sanitized = sanitize_run_name(input_name)
    for ch in ["\\", "/", ":", "*", "?", '"', "<", ">", "|"]:
        assert ch not in sanitized, (
            f"Windows-unsafe char {ch!r} must not appear in sanitized output"
        )
    assert sanitized == "a-b-c-d-e-f-g-h-i-j"


def test_a_strips_reserved_chars_combined_with_timestamp() -> None:
    directory = get_run_data_dir(
        "base", "Fix:Quality/Gate*Mismatch?", "2026-04-10T09:13:58.123Z"
    )
    dir_name = os.path.basename(directory)
    for ch in [":", ".", "*", "?", '"', "<", ">", "|"]:
        assert ch not in dir_name, (
            f"final dir name must not contain {ch!r}: got {dir_name!r}"
        )
    assert dir_name == "fix-quality-gate-mismatch-2026-04-10T09-13-58-123Z"


def test_a_collapses_cyrillic_and_pure_unicode_to_unnamed_run() -> None:
    assert sanitize_run_name("тест-фикс") == "unnamed-run"
    assert sanitize_run_name("修复-bug-修复") == "bug"


def test_a_preserves_ascii_letters_adjacent_to_unicode() -> None:
    assert sanitize_run_name("café-fix") == "caf-fix"
    assert sanitize_run_name("rocket-🚀-launch") == "rocket-launch"
    assert sanitize_run_name("修复_bug_修复") == "bug"


def test_a_unnamed_run_for_pure_unicode_punctuation() -> None:
    assert sanitize_run_name("“…—”") == "unnamed-run"


def test_a_timestamp_strips_colon_and_period() -> None:
    result = sanitize_run_timestamp("2026-04-10T09:13:58.123Z")
    assert ":" not in result
    assert "." not in result
    assert result == "2026-04-10T09-13-58-123Z"


# ── (b) Two-run sequence: zero path collisions ───────────────────────


def test_b_distinct_run_names_produce_distinct_run_data_dirs(
    tmp_path: Path,
) -> None:
    run1_dir = os.path.join(str(tmp_path), "runs", "run-1-id")
    run2_dir = os.path.join(str(tmp_path), "runs", "run-2-id")

    data_dir1 = _init_run_data_dir(
        run1_dir, "fix quality gate", "2026-04-10T09:13:58Z"
    )
    data_dir2 = _init_run_data_dir(
        run2_dir, "Migrate Auth / v2", "2026-04-10T10:00:00Z"
    )

    assert data_dir1, "run 1 must have run_data_dir populated"
    assert data_dir2, "run 2 must have run_data_dir populated"

    # Different sanitized names AND different timestamps -> must diverge.
    assert data_dir1 != data_dir2

    runs_base = os.path.join(str(tmp_path), "runs")
    assert data_dir1.startswith(runs_base)
    assert data_dir2.startswith(runs_base)

    assert data_dir1 == os.path.join(
        runs_base, "fix-quality-gate-2026-04-10T09-13-58Z"
    )
    assert data_dir2 == os.path.join(
        runs_base, "migrate-auth-v2-2026-04-10T10-00-00Z"
    )


def test_b_stores_with_identical_filenames_zero_overlap(
    tmp_path: Path,
) -> None:
    run1_dir = os.path.join(str(tmp_path), "runs", "run-1-id")
    run2_dir = os.path.join(str(tmp_path), "runs", "run-2-id")

    data_dir1 = _init_run_data_dir(
        run1_dir, "alpha-run", "2026-04-10T09:00:00Z"
    )
    data_dir2 = _init_run_data_dir(
        run2_dir, "beta-run", "2026-04-10T09:00:00Z"
    )
    assert data_dir1 and data_dir2

    config = _make_config(str(tmp_path))

    writes = [
        "specs",
        "overviews",
        "debates",
        "raw_collections",
        "curated_collections",
    ]

    run1_paths: list[str] = []
    run2_paths: list[str] = []
    for key in writes:
        p1 = _store(
            config, data_dir1, key, "shared.json", {"id": "run-1", "storage_key": key}
        )
        p2 = _store(
            config, data_dir2, key, "shared.json", {"id": "run-2", "storage_key": key}
        )
        run1_paths.append(p1)
        run2_paths.append(p2)

    # Every pairwise combination across the two runs must differ.
    for i in range(len(writes)):
        for j in range(len(writes)):
            assert run1_paths[i] != run2_paths[j], (
                f'run 1 "{writes[i]}" path must not match run 2 "{writes[j]}" path'
            )

    # Every stored file must live under its own run's run_data_dir tree.
    for p in run1_paths:
        assert p.startswith(data_dir1)
        assert os.path.exists(p)
    for p in run2_paths:
        assert p.startswith(data_dir2)
        assert os.path.exists(p)

    # Content did not cross over.
    for p in run1_paths:
        content = json.loads(Path(p).read_text(encoding="utf-8"))
        assert content["id"] == "run-1"
    for p in run2_paths:
        content = json.loads(Path(p).read_text(encoding="utf-8"))
        assert content["id"] == "run-2"


def test_b_run_data_dir_is_stable_across_multiple_stores(
    tmp_path: Path,
) -> None:
    run_dir = os.path.join(str(tmp_path), "runs", "stable-run-id")
    original_data_dir = _init_run_data_dir(
        run_dir, "stable-run", "2026-04-10T09:13:58Z"
    )
    assert original_data_dir

    config = _make_config(str(tmp_path))

    _store(config, original_data_dir, "specs", "one.json", {"n": 1})
    _store(config, original_data_dir, "specs", "two.json", {"n": 2})

    # Both files land under the same run_data_dir, which does not drift.
    assert os.path.exists(os.path.join(original_data_dir, "specs", "one.json"))
    assert os.path.exists(os.path.join(original_data_dir, "specs", "two.json"))
    # Recomputing with the same inputs yields the same dir (no drift).
    assert (
        _init_run_data_dir(run_dir, "stable-run", "2026-04-10T09:13:58Z")
        == original_data_dir
    )


# ── (c) Legacy fallback: pre-Feature-C runs ──────────────────────────


def test_c_list_resolves_legacy_paths_when_run_data_dir_absent(
    tmp_path: Path,
) -> None:
    run_dir = os.path.join(str(tmp_path), "runs", "legacy-run-id")
    # Pre-Feature-C: no run parameters -> run_data_dir stays None.
    data_dir = _init_run_data_dir(run_dir)
    assert data_dir is None, (
        "pre-Feature-C fixture must NOT have run_data_dir populated"
    )

    # Seed the legacy top-level dir.
    specs_dir = os.path.join(str(tmp_path), "specs")
    os.makedirs(specs_dir, exist_ok=True)
    Path(os.path.join(specs_dir, "legacy-a.json")).write_text(
        json.dumps({"id": "a", "layout": "legacy"}, indent=2), encoding="utf-8"
    )
    Path(os.path.join(specs_dir, "legacy-b.json")).write_text(
        json.dumps({"id": "b", "layout": "legacy"}, indent=2), encoding="utf-8"
    )

    config = _make_config(str(tmp_path))
    names = sorted(_list(config, data_dir, "specs"))
    assert names == ["legacy-a.json", "legacy-b.json"], (
        "legacy fallback must list files from {base_dir}/specs/ when "
        "run_data_dir is absent"
    )


def test_c_legacy_fallback_across_multiple_subtype_keys(
    tmp_path: Path,
) -> None:
    run_dir = os.path.join(str(tmp_path), "runs", "legacy-run-id")
    data_dir = _init_run_data_dir(run_dir)
    assert data_dir is None

    seed_paths = [
        ("specs", "specs", "spec.json"),
        ("overviews", "overviews", "ov.json"),
        ("debates", "debates", "db.json"),
        ("raw_collections", os.path.join("collections", "raw"), "rc.json"),
        ("curated_collections", os.path.join("collections", "curated"), "cc.json"),
    ]
    for storage_key, sub_path, file in seed_paths:
        target_dir = os.path.join(str(tmp_path), sub_path)
        os.makedirs(target_dir, exist_ok=True)
        Path(os.path.join(target_dir, file)).write_text(
            json.dumps({"id": storage_key, "layout": "legacy"}, indent=2),
            encoding="utf-8",
        )

    config = _make_config(str(tmp_path))
    for storage_key, _sub_path, file in seed_paths:
        names = _list(config, data_dir, storage_key)
        assert names == [file], (
            f'legacy fallback for storage_key="{storage_key}" must return {file}'
        )


def test_c_legacy_fallback_disabled_when_run_data_dir_set(
    tmp_path: Path,
) -> None:
    run_dir = os.path.join(str(tmp_path), "runs", "feature-c-run-id")
    data_dir = _init_run_data_dir(run_dir, "c-run", "2026-04-10T09:13:58Z")
    assert data_dir, "Feature-C run must have run_data_dir populated"

    # Seed a legacy file that must NOT appear in the run-scoped listing.
    specs_dir = os.path.join(str(tmp_path), "specs")
    os.makedirs(specs_dir, exist_ok=True)
    Path(os.path.join(specs_dir, "legacy-leak.json")).write_text(
        json.dumps({"id": "leak"}), encoding="utf-8"
    )

    config = _make_config(str(tmp_path))
    names = _list(config, data_dir, "specs")
    assert "legacy-leak.json" not in names, (
        "Feature-C run listing must not include legacy-only files"
    )
    assert names == [], (
        "run-scoped directory is empty, so listing must be empty (not leaking legacy)"
    )
