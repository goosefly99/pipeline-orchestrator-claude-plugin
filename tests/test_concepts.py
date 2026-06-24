"""Port of ``legacy-node/tests/concepts.test.ts`` (48 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.concepts``: the
``collect_concepts`` engine (small / chunked sub-paths, focus/depth/cross-ref
prompt content), the overview persistence round-trip
(``save_overview`` / ``list_overviews`` / ``get_overview``), and the Feature-C
per-run helpers (``overview_file_name`` / ``resolve_overviews_dir`` /
``persist_overview``). Paths are built OS-agnostically with ``os.path.join`` and
rooted at the pytest ``tmp_path`` fixture; the random-hex ``overview_id`` and
ISO ``created_date`` are never asserted as literals (only structural / substring
checks, exactly as the Node suite does).
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

from pipeline_orchestrator.collections_store import Author, ResearchItem
from pipeline_orchestrator.concepts import (
    SourceGroup,
    collect_concepts,
    get_overview,
    list_overviews,
    overview_file_name,
    persist_overview,
    resolve_overviews_dir,
    save_overview,
)
from pipeline_orchestrator.models import (
    Concept,
    KnowledgeOverview,
    OverviewSource,
    Theme,
)

# ── Helpers (mirror the TS ``makeItem`` / ``makeOverview`` factories) ─


def make_item(
    *,
    id: str,
    title: str = "",
    content: str = "default content",
    url: str | None = None,
    date: str | None = None,
    author: Author | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, object] | None = None,
) -> ResearchItem:
    """Mirror the TS ``makeItem({ id, ...overrides })``."""
    return ResearchItem(
        id=id,
        title=title,
        content=content,
        url=url,
        date=date,
        author=author,
        tags=tags if tags is not None else [],
        metadata=metadata if metadata is not None else {},
    )


def make_overview(**overrides: object) -> KnowledgeOverview:
    """Mirror the TS ``makeOverview(overrides?)``."""
    base: dict[str, object] = {
        "overview_id": "test-id-1234",
        "title": "Test Overview",
        "created_date": "2026-01-01T00:00:00.000Z",
        "status": "draft",
        "sources": [],
        "summary": "A test summary",
        "concepts": [],
        "themes": [],
        "key_findings": [],
        "knowledge_gaps": [],
        "open_questions": [],
    }
    base.update(overrides)
    return KnowledgeOverview(**base)  # type: ignore[arg-type]


def grp(collection: str, items: list[ResearchItem]) -> SourceGroup:
    """Build a :class:`SourceGroup` (mirrors the inline TS object literal)."""
    return SourceGroup(collection=collection, items=items)


# ── collectConcepts ───────────────────────────────────────────────


def test_returns_a_template_and_synthesis_prompt() -> None:
    result = collect_concepts(
        [grp("test-col", [make_item(id="i1", content="hello")])],
        title="My Overview",
    )
    assert result.template
    assert result.synthesis_prompt
    assert isinstance(result.synthesis_prompt, str)
    assert len(result.synthesis_prompt) > 0


def test_template_has_correct_structure() -> None:
    result = collect_concepts(
        [grp("c1", [make_item(id="i1")])],
        title="Structure Test",
    )
    t = result.template

    assert t.title == "Structure Test"
    assert t.status == "draft"
    assert len(t.overview_id) > 0
    assert len(t.created_date) > 0
    assert isinstance(t.sources, list)
    assert isinstance(t.concepts, list)
    assert isinstance(t.themes, list)
    assert t.summary == ""
    assert t.concepts == []
    # themes seeds a placeholder object so the AI knows the required field names.
    assert len(t.themes) > 0, (
        "themes must have a placeholder for concept_names guidance"
    )
    placeholder = t.themes[0]
    assert hasattr(placeholder, "concept_names"), (
        "placeholder must have concept_names key"
    )


def test_template_sources_list_all_items_from_all_groups() -> None:
    result = collect_concepts(
        [
            grp("col-a", [make_item(id="a1"), make_item(id="a2")]),
            grp("col-b", [make_item(id="b1")]),
        ],
        title="Multi-source",
    )

    assert len(result.template.sources) == 3
    assert any(
        s.item_id == "a1" and s.collection == "col-a" for s in result.template.sources
    )
    assert any(
        s.item_id == "b1" and s.collection == "col-b" for s in result.template.sources
    )


def test_synthesis_prompt_includes_the_title() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="i")])],
        title="Unique Title XYZ",
    )
    assert "Unique Title XYZ" in result.synthesis_prompt


def test_synthesis_prompt_includes_source_item_content() -> None:
    result = collect_concepts(
        [
            grp(
                "c",
                [make_item(id="i1", content="Specific research finding about markets")],
            )
        ],
        title="Content Test",
    )
    assert "Specific research finding about markets" in result.synthesis_prompt


def test_synthesis_prompt_includes_item_id_for_reference() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="item-abc-123", content="data")])],
        title="ID Test",
    )
    assert "item-abc-123" in result.synthesis_prompt


def test_synthesis_prompt_includes_focus_clause_when_provided() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="i")])],
        title="Focus Test",
        focus="risk management",
    )
    assert "risk management" in result.synthesis_prompt
    assert "Focus area" in result.synthesis_prompt


def test_synthesis_prompt_omits_focus_clause_when_not_provided() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="i")])],
        title="No Focus",
    )
    assert "Focus area" not in result.synthesis_prompt


def test_respects_depth_parameter_in_instructions() -> None:
    brief = collect_concepts(
        [grp("c", [make_item(id="i")])],
        title="Brief",
        depth="brief",
    )
    assert "3-5 top-level concepts" in brief.synthesis_prompt

    deep = collect_concepts(
        [grp("c", [make_item(id="i")])],
        title="Deep",
        depth="deep",
    )
    assert "every distinct concept" in deep.synthesis_prompt


def test_includes_author_info_in_prompt_when_present() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="i", author=Author(name="Alice", handle="@alice"))])],
        title="Author Test",
    )
    assert "Alice" in result.synthesis_prompt
    assert "@alice" in result.synthesis_prompt


def test_includes_tags_in_prompt_when_present() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="i", tags=["ml", "finance"])])],
        title="Tags Test",
    )
    assert "ml" in result.synthesis_prompt
    assert "finance" in result.synthesis_prompt


def test_includes_cross_reference_analysis_for_multiple_items_with_shared_tags() -> (
    None
):
    result = collect_concepts(
        [
            grp(
                "c",
                [
                    make_item(id="i1", tags=["shared-tag", "unique1"]),
                    make_item(id="i2", tags=["shared-tag", "unique2"]),
                ],
            )
        ],
        title="CrossRef Test",
    )
    assert "Cross-Reference" in result.synthesis_prompt
    assert "shared-tag" in result.synthesis_prompt


def test_includes_json_template_in_prompt_for_llm_to_fill() -> None:
    result = collect_concepts(
        [grp("c", [make_item(id="i")])],
        title="Template Test",
    )
    assert '"overview_id"' in result.synthesis_prompt
    assert '"concepts"' in result.synthesis_prompt
    assert '"themes"' in result.synthesis_prompt


# ── collectConcepts — chunked mode ────────────────────────────────


def _make_items(count: int) -> list[ResearchItem]:
    return [
        make_item(id=f"item-{i + 1}", content=f"content for item {i + 1}")
        for i in range(count)
    ]


def test_small_collection_returns_no_chunk_field() -> None:
    items = _make_items(10)
    result = collect_concepts([grp("col", items)], title="Small Collection")

    assert result.chunk is None
    assert len(result.synthesis_prompt) > 0
    assert result.template


def test_large_collection_20_items_activates_chunk_mode_first_chunk() -> None:
    items = _make_items(20)
    result = collect_concepts([grp("col", items)], title="Large Collection")

    assert result.chunk, "chunk field should be present"
    assert result.chunk.chunk_index == 0
    assert result.chunk.item_count == 8
    assert result.chunk.total_items == 20
    assert result.chunk.total_chunks == 3
    assert result.chunk.is_final_chunk is False
    assert isinstance(result.chunk.continuation_token, str), (
        "continuation_token should be set"
    )


def test_continuation_token_is_base64_encoding_of_next_chunk_index() -> None:
    items = _make_items(20)
    result = collect_concepts([grp("col", items)], title="Token Test")

    assert result.chunk
    assert result.chunk.continuation_token is not None
    decoded = base64.b64decode(result.chunk.continuation_token).decode("utf-8")
    assert decoded == "1"


def test_last_chunk_has_is_final_chunk_true_and_no_continuation_token() -> None:
    items = _make_items(20)
    # 20 items, CHUNK_SIZE=8: chunk 0 = 0-7, chunk 1 = 8-15, chunk 2 = 16-19 (final).
    result = collect_concepts([grp("col", items)], title="Final Chunk", chunk_index=2)

    assert result.chunk
    assert result.chunk.is_final_chunk is True
    assert result.chunk.continuation_token is None
    assert result.chunk.item_count == 4  # items 16-19


def test_chunk_index_1_processes_items_8_to_15_second_chunk() -> None:
    items = _make_items(20)
    result = collect_concepts([grp("col", items)], title="Second Chunk", chunk_index=1)

    assert result.chunk
    assert result.chunk.chunk_index == 1
    assert result.chunk.item_count == 8
    assert result.chunk.is_final_chunk is False
    # Items 8-15 should appear in the prompt.
    assert "item-9" in result.synthesis_prompt  # 1-indexed
    assert "item-16" in result.synthesis_prompt
    assert "item-1\n" not in result.synthesis_prompt
    assert "[item-1]" not in result.synthesis_prompt


def test_total_chunks_is_ceil_total_items_over_8() -> None:
    for count, expected in [(16, 2), (17, 3), (24, 3), (25, 4)]:
        items = _make_items(count)
        result = collect_concepts([grp("col", items)], title=f"Count {count}")
        assert result.chunk, f"chunk expected for count {count}"
        assert result.chunk.total_chunks == expected, f"total_chunks for {count} items"


def test_synthesis_prompt_includes_chunk_header_when_chunking_is_active() -> None:
    items = _make_items(20)
    result = collect_concepts([grp("col", items)], title="Chunk Header Test")

    assert result.chunk
    assert "Chunk:" in result.synthesis_prompt
    assert "of 3" in result.synthesis_prompt
    assert "of 20" in result.synthesis_prompt


def test_no_chunk_header_in_prompt_for_small_collection() -> None:
    items = _make_items(5)
    result = collect_concepts([grp("col", items)], title="No Chunk Header")

    assert result.chunk is None
    assert "**Chunk:**" not in result.synthesis_prompt


def test_chunk_continuation_round_trip() -> None:
    items = _make_items(20)

    # Step 1: fetch chunk 0 (no chunk_index provided, defaults to 0).
    chunk0 = collect_concepts([grp("col", items)], title="Round-Trip Test")

    assert chunk0.chunk, "chunk 0 should have chunk metadata"
    assert chunk0.chunk.chunk_index == 0
    assert isinstance(chunk0.chunk.continuation_token, str), (
        "chunk 0 should have a continuation_token"
    )

    # Step 2: decode the token to get the next chunk index.
    token = chunk0.chunk.continuation_token
    assert token is not None
    decoded = base64.b64decode(token).decode("utf-8")
    next_chunk_index = int(decoded)
    assert next_chunk_index == 1, "decoded token should indicate chunk index 1"

    # Step 3: fetch chunk 1 using the decoded index.
    chunk1 = collect_concepts(
        [grp("col", items)], title="Round-Trip Test", chunk_index=next_chunk_index
    )

    assert chunk1.chunk, "chunk 1 should have chunk metadata"
    assert chunk1.chunk.chunk_index == 1
    assert chunk1.chunk.item_count == 8, "chunk 1 should contain 8 items (items 8-15)"
    assert chunk1.chunk.is_final_chunk is False, "chunk 1 should not be the final chunk"
    assert isinstance(chunk1.chunk.continuation_token, str), (
        "chunk 1 should provide a token for chunk 2"
    )

    # Step 4: verify chunk 1 prompt contains items from the correct slice.
    assert "item-9" in chunk1.synthesis_prompt, "chunk 1 prompt should include item-9"
    assert "item-16" in chunk1.synthesis_prompt, "chunk 1 prompt should include item-16"


# ── saveOverview / listOverviews / getOverview round-trip ─────────


def test_saves_an_overview_to_disk_and_returns_the_file_path(tmp_path: Path) -> None:
    overview = make_overview(title="Save Test")
    path = save_overview(overview, str(tmp_path))

    assert os.path.exists(path)
    assert "save-test" in path
    assert path.endswith(".json")


def test_creates_the_output_directory_if_it_does_not_exist(tmp_path: Path) -> None:
    sub_dir = os.path.join(str(tmp_path), "nested", "output")
    overview = make_overview(title="Dir Create")
    path = save_overview(overview, sub_dir)

    assert os.path.exists(path)
    assert path.startswith(sub_dir)


def test_generates_slug_from_title(tmp_path: Path) -> None:
    overview = make_overview(title="My Complex Title! @#$")
    path = save_overview(overview, str(tmp_path))

    assert "my-complex-title" in path
    # Special chars stripped.
    assert "@" not in path
    assert "#" not in path


def test_lists_saved_overviews_with_metadata(tmp_path: Path) -> None:
    save_overview(
        make_overview(
            title="List Test A",
            overview_id="ov-aaa",
            sources=[OverviewSource(collection="c", item_id="i", title="t")],
        ),
        str(tmp_path),
    )
    save_overview(
        make_overview(title="List Test B", overview_id="ov-bbb"),
        str(tmp_path),
    )

    lst = list_overviews(str(tmp_path))
    assert len(lst) == 2

    a = next((entry for entry in lst if entry["overview_id"] == "ov-aaa"), None)
    assert a
    assert a["title"] == "List Test A"
    assert a["source_count"] == 1


def test_returns_empty_array_for_nonexistent_directory(tmp_path: Path) -> None:
    lst = list_overviews(os.path.join(str(tmp_path), "nope"))
    assert lst == []


def test_retrieves_a_saved_overview_by_id(tmp_path: Path) -> None:
    overview = make_overview(overview_id="ov-retrieve-me", title="Retrieve")
    save_overview(overview, str(tmp_path))

    loaded = get_overview("ov-retrieve-me", str(tmp_path))
    assert loaded
    assert loaded.overview_id == "ov-retrieve-me"
    assert loaded.title == "Retrieve"


def test_returns_null_for_nonexistent_id(tmp_path: Path) -> None:
    save_overview(make_overview(overview_id="ov-other"), str(tmp_path))
    loaded = get_overview("ov-doesnt-exist", str(tmp_path))
    assert loaded is None


def test_get_overview_returns_null_for_nonexistent_directory(tmp_path: Path) -> None:
    loaded = get_overview("anything", os.path.join(str(tmp_path), "nope"))
    assert loaded is None


def test_round_trips_save_then_load_preserves_all_fields(tmp_path: Path) -> None:
    overview = make_overview(
        overview_id="ov-roundtrip",
        title="Round Trip",
        summary="Test summary",
        concepts=[
            Concept(
                name="C1",
                category="test",
                description="desc",
                key_details=["d1"],
                source_items=["i1"],
                relationships=[],
            )
        ],
        themes=[Theme(name="T1", description="theme", concept_names=["C1"])],
        key_findings=["finding1"],
        knowledge_gaps=["gap1"],
        open_questions=["q1"],
    )
    save_overview(overview, str(tmp_path))

    loaded = get_overview("ov-roundtrip", str(tmp_path))
    assert loaded
    assert loaded.summary == "Test summary"
    assert len(loaded.concepts) == 1
    assert loaded.concepts[0].name == "C1"
    assert len(loaded.themes) == 1
    assert len(loaded.key_findings) == 1
    assert len(loaded.knowledge_gaps) == 1
    assert len(loaded.open_questions) == 1


# ── overviewFileName ──────────────────────────────────────────────


def test_derives_a_slug_based_filename_with_the_overview_id_appended() -> None:
    overview = make_overview(overview_id="abc1234", title="My Overview")
    assert overview_file_name(overview) == "my-overview--abc1234.json"


def test_strips_special_characters_and_collapses_runs_of_non_alphanumerics() -> None:
    overview = make_overview(overview_id="xyz", title="Hello! @World # 2026")
    assert overview_file_name(overview) == "hello-world-2026--xyz.json"


def test_trims_leading_and_trailing_hyphens_from_the_slug() -> None:
    overview = make_overview(overview_id="id", title="!!!trim me!!!")
    assert overview_file_name(overview) == "trim-me--id.json"


def test_truncates_slug_to_at_most_60_characters() -> None:
    overview = make_overview(overview_id="id", title="a" * 100)
    name = overview_file_name(overview)
    # slug is 60 'a's, then '--id.json'.
    assert name == f"{'a' * 60}--id.json"


# ── resolveOverviewsDir ───────────────────────────────────────────


def test_routes_to_run_data_dir_overviews_when_run_data_dir_is_set(
    tmp_path: Path,
) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "my-run-2026-04-10")
    directory = resolve_overviews_dir(
        run_data_dir, os.path.join(str(tmp_path), "legacy")
    )
    assert directory == os.path.join(run_data_dir, "overviews")


def test_falls_back_to_legacy_base_dir_overviews_when_run_data_dir_is_undefined(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    directory = resolve_overviews_dir(None, legacy_base_dir)
    assert directory == os.path.join(legacy_base_dir, "overviews")


def test_treats_empty_string_run_data_dir_as_absent_and_falls_back(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    directory = resolve_overviews_dir("", legacy_base_dir)
    assert directory == os.path.join(legacy_base_dir, "overviews")


# ── persistOverview ───────────────────────────────────────────────


def test_writes_under_run_data_dir_overviews_when_run_data_dir_is_set(
    tmp_path: Path,
) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "persist-a")
    overview = make_overview(overview_id="ov-persist-1", title="Persist Test")

    written_path = persist_overview(
        run_data_dir, os.path.join(str(tmp_path), "legacy"), overview
    )

    expected = os.path.join(
        run_data_dir, "overviews", "persist-test--ov-persist-1.json"
    )
    assert written_path == expected
    assert os.path.exists(expected), f"expected file at {expected}"
    with open(expected, encoding="utf-8") as fh:
        parsed = json.load(fh)
    assert parsed["overview_id"] == "ov-persist-1"
    assert parsed["title"] == "Persist Test"


def test_falls_back_to_legacy_base_dir_overviews_when_run_data_dir_is_absent(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-base")
    overview = make_overview(overview_id="ov-legacy-1", title="Legacy Test")

    written_path = persist_overview(None, legacy_base_dir, overview)

    expected = os.path.join(
        legacy_base_dir, "overviews", "legacy-test--ov-legacy-1.json"
    )
    assert written_path == expected
    assert os.path.exists(expected), f"expected file at {expected}"


def test_persist_falls_back_to_legacy_base_dir_when_run_data_dir_is_empty_string(
    tmp_path: Path,
) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-empty")
    overview = make_overview(overview_id="ov-empty", title="Empty Rd")

    written_path = persist_overview("", legacy_base_dir, overview)

    expected = os.path.join(legacy_base_dir, "overviews", "empty-rd--ov-empty.json")
    assert written_path == expected
    assert os.path.exists(expected)


def test_honors_output_dir_override_above_both_run_data_dir_and_legacy_base_dir(
    tmp_path: Path,
) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "should-be-ignored")
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-ignored")
    override_dir = os.path.join(str(tmp_path), "explicit-override")
    overview = make_overview(overview_id="ov-override", title="Override Test")

    written_path = persist_overview(
        run_data_dir, legacy_base_dir, overview, override_dir
    )

    expected = os.path.join(
        os.path.abspath(override_dir), "override-test--ov-override.json"
    )
    assert written_path == expected
    assert os.path.exists(expected)
    assert not os.path.exists(os.path.join(run_data_dir, "overviews")), (
        "runDataDir path should not be created"
    )
    assert not os.path.exists(os.path.join(legacy_base_dir, "overviews")), (
        "legacyBaseDir path should not be created"
    )


def test_honors_output_dir_override_even_when_run_data_dir_is_absent(
    tmp_path: Path,
) -> None:
    override_dir = os.path.join(str(tmp_path), "override-no-run")
    overview = make_overview(overview_id="ov-onr", title="No Run Override")

    written_path = persist_overview(
        None, os.path.join(str(tmp_path), "legacy"), overview, override_dir
    )

    expected = os.path.join(
        os.path.abspath(override_dir), "no-run-override--ov-onr.json"
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

    overview = make_overview(overview_id="ov-rel", title="Relative")
    written_path = persist_overview(None, str(tmp_path), overview, relative_override)

    expected = os.path.join(os.path.abspath(relative_override), "relative--ov-rel.json")
    assert written_path == expected
    assert os.path.isabs(written_path), "result should be an absolute path"
    assert os.path.exists(written_path)


def test_creates_the_target_directory_tree_if_it_does_not_exist(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "deep", "nested", "target")
    overview = make_overview(overview_id="ov-deep", title="Deep Nested")

    persist_overview(run_data_dir, str(tmp_path), overview)

    directory = os.path.join(run_data_dir, "overviews")
    assert os.path.exists(directory), f"expected mkdir to create {directory}"


def test_persist_returns_an_absolute_path(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "abs")
    overview = make_overview(overview_id="ov-abs", title="Abs Path")

    written_path = persist_overview(run_data_dir, str(tmp_path), overview)

    assert os.path.abspath(written_path) == written_path


def test_overwrites_an_existing_overview_file_without_raising(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "overwrite-ov")
    first = make_overview(
        overview_id="ov-same", title="Same Title", summary="first version"
    )
    second = make_overview(
        overview_id="ov-same", title="Same Title", summary="second version"
    )

    path1 = persist_overview(run_data_dir, str(tmp_path), first)
    path2 = persist_overview(run_data_dir, str(tmp_path), second)

    assert path1 == path2
    with open(path2, encoding="utf-8") as fh:
        parsed = json.load(fh)
    assert parsed["summary"] == "second version", (
        "second write should overwrite the first"
    )


def test_uses_the_same_filename_as_save_overview(tmp_path: Path) -> None:
    overview = make_overview(overview_id="ov-match", title="Match Test")

    saved_path = save_overview(overview, str(tmp_path))
    persisted_path = persist_overview(None, str(tmp_path), overview)

    # saveOverview writes directly into its outputDir arg; persistOverview treats
    # legacyBaseDir as a parent and appends 'overviews/'. Both derive the same
    # on-disk filename from the shared overviewFileName helper.
    expected_name = overview_file_name(overview)
    assert saved_path == os.path.join(str(tmp_path), expected_name)
    assert persisted_path == os.path.join(str(tmp_path), "overviews", expected_name)


# ── Regression: ensure_ascii=False parity for on-disk overview bytes ─


def test_save_overview_writes_raw_utf8_not_unicode_escapes(tmp_path: Path) -> None:
    """``save_overview`` must write raw UTF-8 (``ensure_ascii=False``) to match
    Node's ``JSON.stringify(overview, null, 2)`` + ``writeFileSync(..., 'utf-8')``.

    Pre-fix (Python's default ``ensure_ascii=True``) this FAILS: ``é``/``—`` in
    the title/summary land on disk as ``\\u00e9``/``\\u2014``. Reading the raw
    bytes asserts they are stored as literal UTF-8 characters instead.
    """
    overview = make_overview(
        overview_id="ov-nonascii",
        title="Café — Études",
        summary="Naïve résumé — façade",
    )
    path = save_overview(overview, str(tmp_path))

    raw = Path(path).read_text(encoding="utf-8")
    assert "é" in raw and "—" in raw, "raw UTF-8 chars must be present on disk"
    assert "\\u00e9" not in raw, "é must not be \\uXXXX-escaped on disk"
    assert "\\u2014" not in raw, "em-dash must not be \\uXXXX-escaped on disk"
    # Round-trip still loads correctly.
    loaded = get_overview("ov-nonascii", str(tmp_path))
    assert loaded
    assert loaded.title == "Café — Études"


def test_persist_overview_writes_raw_utf8_not_unicode_escapes(tmp_path: Path) -> None:
    """``persist_overview`` mirrors the same raw-UTF-8 on-disk contract."""
    overview = make_overview(
        overview_id="ov-persist-nonascii",
        title="Über—Naïve",
        summary="café — déjà vu",
    )
    written_path = persist_overview(None, str(tmp_path), overview)

    raw = Path(written_path).read_text(encoding="utf-8")
    assert "Ü" in raw and "—" in raw and "é" in raw
    assert "\\u00dc" not in raw, "Ü must not be \\uXXXX-escaped on disk"
    assert "\\u2014" not in raw, "em-dash must not be \\uXXXX-escaped on disk"
