"""Port of ``legacy-node/tests/synth.test.ts`` (20 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.synth``: the
``spec_file_name`` slug helper, the Feature-C per-run helpers
(``resolve_specs_dir`` / ``persist_spec``), and the legacy ``save_spec`` path.
Paths are built OS-agnostically with ``os.path.join`` and rooted at the pytest
``tmp_path`` fixture; the spec ``updated_date`` mutation is asserted against
``_now_date`` (never a hard-coded literal), exactly as the Node suite asserts
against ``new Date().toISOString().split('T')[0]``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline_orchestrator.models import DesignSpec
from pipeline_orchestrator.synth import (
    _now_date,
    persist_spec,
    resolve_specs_dir,
    save_spec,
    spec_file_name,
)

# ── Helper (mirrors the TS ``makeSpec(overrides?)``) ───────────────


def make_spec(**overrides: object) -> DesignSpec:
    """Mirror the TS ``makeSpec({...overrides})`` factory."""
    base: dict[str, object] = {
        "spec_id": "12345678-90ab-cdef-1234-567890abcdef",
        "title": "Test Spec",
        "created_date": "2026-04-10",
        "updated_date": "2026-04-10",
        "version": "1.0",
        "status": "draft",
        "spec_type": "implementation",
        "sources": [],
        "notes": "",
    }
    base.update(overrides)
    return DesignSpec(**base)  # type: ignore[arg-type]


# ── specFileName ──────────────────────────────────────────────────


def test_derives_a_slug_based_filename_with_first_8_chars_of_spec_id_appended() -> None:
    spec = make_spec(spec_id="abcdef12-rest-of-uuid", title="My Spec")
    assert spec_file_name(spec) == "my-spec--abcdef12.json"


def test_strips_special_characters_and_collapses_runs_of_non_alphanumerics() -> None:
    spec = make_spec(spec_id="xyzxyzxy-rest", title="Hello! @World # 2026")
    assert spec_file_name(spec) == "hello-world-2026--xyzxyzxy.json"


def test_trims_leading_and_trailing_hyphens_from_the_slug() -> None:
    spec = make_spec(spec_id="11111111-rest", title="!!!trim me!!!")
    assert spec_file_name(spec) == "trim-me--11111111.json"


def test_truncates_slug_to_at_most_60_characters() -> None:
    spec = make_spec(spec_id="22222222-rest", title="a" * 100)
    name = spec_file_name(spec)
    # slug is 60 'a's, then '--22222222.json'
    assert name == f"{'a' * 60}--22222222.json"


# ── resolveSpecsDir ───────────────────────────────────────────────


def test_routes_to_run_data_dir_specs_when_run_data_dir_is_set(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "my-run-2026-04-10")
    dir_ = resolve_specs_dir(run_data_dir, os.path.join(str(tmp_path), "legacy"))
    assert dir_ == os.path.join(run_data_dir, "specs")


def test_falls_back_to_legacy_base_dir_specs_when_run_data_dir_is_none(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    dir_ = resolve_specs_dir(None, legacy_base_dir)
    assert dir_ == os.path.join(legacy_base_dir, "specs")


def test_treats_empty_string_run_data_dir_as_absent_falsy_and_falls_back(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    dir_ = resolve_specs_dir("", legacy_base_dir)
    assert dir_ == os.path.join(legacy_base_dir, "specs")


# ── persistSpec ───────────────────────────────────────────────────


def test_writes_under_run_data_dir_specs_when_run_data_dir_is_set(
    tmp_path: Path,
) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "persist-a")
    spec = make_spec(
        spec_id="aaaaaaaa-1111-2222-3333-444444444444", title="Persist Test"
    )

    written_path = persist_spec(
        run_data_dir, os.path.join(str(tmp_path), "legacy"), spec
    )

    expected = os.path.join(run_data_dir, "specs", "persist-test--aaaaaaaa.json")
    assert written_path == expected
    assert os.path.exists(expected), f"expected file at {expected}"
    with open(expected, encoding="utf-8") as fh:
        parsed = json.load(fh)
    assert parsed["spec_id"] == "aaaaaaaa-1111-2222-3333-444444444444"
    assert parsed["title"] == "Persist Test"


def test_falls_back_to_legacy_base_dir_specs_when_run_data_dir_is_absent(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-base")
    spec = make_spec(
        spec_id="bbbbbbbb-1111-2222-3333-444444444444", title="Legacy Test"
    )

    written_path = persist_spec(None, legacy_base_dir, spec)

    expected = os.path.join(legacy_base_dir, "specs", "legacy-test--bbbbbbbb.json")
    assert written_path == expected
    assert os.path.exists(expected), f"expected file at {expected}"


def test_falls_back_to_legacy_base_dir_when_run_data_dir_is_an_empty_string(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-empty")
    spec = make_spec(spec_id="cccccccc-1111-2222-3333-444444444444", title="Empty Rd")

    written_path = persist_spec("", legacy_base_dir, spec)

    expected = os.path.join(legacy_base_dir, "specs", "empty-rd--cccccccc.json")
    assert written_path == expected
    assert os.path.exists(expected)


def test_honors_output_dir_override_above_both_run_data_dir_and_legacy_base_dir(
    tmp_path: Path,
) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "should-be-ignored")
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-ignored")
    override_dir = os.path.join(str(tmp_path), "explicit-override")
    spec = make_spec(
        spec_id="dddddddd-1111-2222-3333-444444444444", title="Override Test"
    )

    written_path = persist_spec(run_data_dir, legacy_base_dir, spec, override_dir)

    expected = os.path.join(
        os.path.abspath(override_dir), "override-test--dddddddd.json"
    )
    assert written_path == expected
    assert os.path.exists(expected)
    assert not os.path.exists(
        os.path.join(run_data_dir, "specs")
    ), "runDataDir path should not be created"
    assert not os.path.exists(
        os.path.join(legacy_base_dir, "specs")
    ), "legacyBaseDir path should not be created"


def test_honors_output_dir_override_even_when_run_data_dir_is_absent(
    tmp_path: Path,
) -> None:
    override_dir = os.path.join(str(tmp_path), "override-no-run")
    spec = make_spec(
        spec_id="eeeeeeee-1111-2222-3333-444444444444", title="No Run Override"
    )

    written_path = persist_spec(
        None, os.path.join(str(tmp_path), "legacy"), spec, override_dir
    )

    expected = os.path.join(
        os.path.abspath(override_dir), "no-run-override--eeeeeeee.json"
    )
    assert written_path == expected
    assert os.path.exists(expected)


def test_resolves_a_relative_output_dir_override_to_an_absolute_path(
    tmp_path: Path,
) -> None:
    # Compute an actually-relative path from cwd to a subdir of tmp_path so the
    # test exercises the abspath() call path, not the absolute-passthrough.
    absolute_override = os.path.join(str(tmp_path), "rel-override")
    relative_override = os.path.relpath(absolute_override, os.getcwd())
    assert not os.path.isabs(relative_override), "test setup: override must be relative"

    spec = make_spec(spec_id="ffffffff-1111-2222-3333-444444444444", title="Relative")
    written_path = persist_spec(None, str(tmp_path), spec, relative_override)

    expected = os.path.join(
        os.path.abspath(relative_override), "relative--ffffffff.json"
    )
    assert written_path == expected
    assert os.path.isabs(written_path), "result should be an absolute path"
    assert os.path.exists(written_path)


def test_creates_the_target_directory_tree_if_it_does_not_exist(
    tmp_path: Path,
) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "deep", "nested", "target")
    spec = make_spec(
        spec_id="99999999-1111-2222-3333-444444444444", title="Deep Nested"
    )

    persist_spec(run_data_dir, str(tmp_path), spec)

    dir_ = os.path.join(run_data_dir, "specs")
    assert os.path.exists(dir_), f"expected mkdir to create {dir_}"


def test_returns_an_absolute_path(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "abs")
    spec = make_spec(spec_id="88888888-1111-2222-3333-444444444444", title="Abs Path")

    written_path = persist_spec(run_data_dir, str(tmp_path), spec)

    assert os.path.abspath(written_path) == written_path


def test_overwrites_an_existing_spec_file_without_raising(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "overwrite-spec")
    first = make_spec(
        spec_id="77777777-1111-2222-3333-444444444444",
        title="Same Title",
        notes="first version",
    )
    second = make_spec(
        spec_id="77777777-1111-2222-3333-444444444444",
        title="Same Title",
        notes="second version",
    )

    path1 = persist_spec(run_data_dir, str(tmp_path), first)
    path2 = persist_spec(run_data_dir, str(tmp_path), second)

    assert path1 == path2
    with open(path2, encoding="utf-8") as fh:
        parsed = json.load(fh)
    assert parsed["notes"] == "second version", (
        "second write should overwrite the first"
    )


def test_uses_the_same_filename_as_save_spec_shared_helper(tmp_path: Path) -> None:
    spec = make_spec(spec_id="66666666-1111-2222-3333-444444444444", title="Match Test")

    saved_path = save_spec(spec, str(tmp_path))
    persisted_path = persist_spec(None, str(tmp_path), spec)

    # save_spec writes directly into its output_dir arg; persist_spec treats
    # legacy_base_dir as a parent and appends 'specs/'. The intent of this check
    # is to confirm both routes derive the same on-disk filename from the shared
    # spec_file_name helper — the containing directories intentionally differ.
    expected_name = spec_file_name(spec)
    assert saved_path == os.path.join(str(tmp_path), expected_name)
    assert persisted_path == os.path.join(str(tmp_path), "specs", expected_name)


def test_mutates_spec_updated_date_to_today_before_writing(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "updated-date")
    spec = make_spec(
        spec_id="55555555-1111-2222-3333-444444444444",
        title="Date Mutation",
        updated_date="1999-01-01",
    )

    persist_spec(run_data_dir, str(tmp_path), spec)

    today = _now_date()
    assert spec.updated_date == today, (
        "persist_spec should mutate updated_date to today"
    )

    with open(
        os.path.join(run_data_dir, "specs", "date-mutation--55555555.json"),
        encoding="utf-8",
    ) as fh:
        written = json.load(fh)
    assert written["updated_date"] == today


# ── saveSpec ──────────────────────────────────────────────────────


def test_writes_to_the_supplied_output_dir_using_the_same_filename(
    tmp_path: Path,
) -> None:
    spec = make_spec(spec_id="44444444-1111-2222-3333-444444444444", title="Save Test")

    written_path = save_spec(spec, str(tmp_path))

    expected = os.path.join(str(tmp_path), spec_file_name(spec))
    assert written_path == expected
    assert os.path.exists(expected)


def test_creates_the_output_dir_if_it_does_not_exist(tmp_path: Path) -> None:
    target_dir = os.path.join(str(tmp_path), "does", "not", "yet", "exist")
    spec = make_spec(spec_id="33333333-1111-2222-3333-444444444444", title="Create Dir")

    written_path = save_spec(spec, target_dir)

    assert os.path.exists(target_dir)
    assert os.path.exists(written_path)
