"""Port of ``legacy-node/tests/cc-handlers.test.ts`` (14 cases) + a registration
integrity test.

The 14 ported cases exercise the ``handle_cc_*`` handlers directly through a
mocked :class:`CCContext` (the DI seam) — byte-exact string assertions on the
plain-text line blocks (content truncation, the no-items / missing-arg paths,
and the Feature-C ``persist_overview`` vs ``save_overview`` routing) plus the
theme-template correctness checks shared with the engine.

The added Python-native integrity test builds a fresh ``FastMCP("pipeline")``,
calls :func:`register_cc_tools`, and asserts (via ``asyncio.run(mcp.list_tools())``)
that exactly the seven ``pipeline_cc_*`` tools register with the spec'd
``_meta.max_result_chars`` (§5.4) and that ``pipeline_cc_load_collection`` carries
no annotations while the others carry the spec'd ``ToolAnnotations`` subset (§4.3).
``register_cc_tools`` is NOT wired into the global ``register_tools`` seam — this
test constructs its own FastMCP instance, so the global handshake stays at 0.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest

from pipeline_orchestrator.collections_store import ResearchItem
from pipeline_orchestrator.concepts import collect_concepts
from pipeline_orchestrator.models import (
    HandlerResponse,
    KnowledgeOverview,
    Theme,
)
from pipeline_orchestrator.tools.cc_tools import (
    CCContext,
    handle_cc_collect_concepts,
    handle_cc_get_items,
    handle_cc_get_overview,
    handle_cc_query,
    handle_cc_save_overview,
    register_cc_tools,
)

# ── Helpers (mirror the TS ``makeItem`` / ``makeCtx`` / ``makeOverview``) ─


def make_item(id: str, content: str) -> ResearchItem:
    return ResearchItem(
        id=id,
        title=f"Item {id}",
        content=content,
        tags=[],
        metadata={},
    )


def make_ctx(items: list[ResearchItem]) -> CCContext:
    """Mirror the TS ``makeCtx(items)`` — a CCContext with all callables stubbed."""

    def _not_impl(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("not implemented")

    return CCContext(
        load_collection=_not_impl,
        query_items=lambda *a, **k: [],
        get_items=lambda _collection, ids: [i for i in items if i.id in ids],
        collect_concepts=lambda *a, **k: _StubResult(),
        save_overview=lambda *a, **k: "",
        list_overviews=lambda *a, **k: [],
        get_overview=lambda *a, **k: None,
    )


class _StubResult:
    """Mirror the TS ``collectConcepts: () => ({ synthesis_prompt: '' })`` stub."""

    synthesis_prompt = ""
    chunk = None


def make_overview(**overrides: object) -> KnowledgeOverview:
    base: dict[str, object] = {
        "overview_id": "test-id",
        "title": "Test Overview",
        "created_date": "2026-01-01T00:00:00.000Z",
        "status": "draft",
        "sources": [],
        "summary": "",
        "concepts": [],
        "themes": [],
        "key_findings": [],
        "knowledge_gaps": [],
        "open_questions": [],
    }
    base.update(overrides)
    return KnowledgeOverview(**base)  # type: ignore[arg-type]


# ── handleCCGetItems — content truncation ─────────────────────────


def test_truncates_content_to_500_chars_by_default_when_content_exceeds_limit() -> None:
    long_content = "A" * 600
    ctx = make_ctx([make_item("i1", long_content)])

    result = handle_cc_get_items({"collection": "test", "item_ids": ["i1"]}, ctx)

    assert "A" * 500 + "..." in result.json, (
        "should contain 500 As followed by ellipsis"
    )
    assert "A" * 501 not in result.json, "should not contain 501+ consecutive As"


def test_returns_content_as_is_when_content_is_under_the_default_500_char_limit() -> (
    None
):
    short_content = "Hello world"
    ctx = make_ctx([make_item("i1", short_content)])

    result = handle_cc_get_items({"collection": "test", "item_ids": ["i1"]}, ctx)

    assert "Hello world" in result.json, "should include full short content"
    assert "..." not in result.json, "should not append ellipsis for short content"


def test_respects_max_content_length_override() -> None:
    content = "B" * 200
    ctx = make_ctx([make_item("i1", content)])

    result = handle_cc_get_items(
        {"collection": "test", "item_ids": ["i1"], "max_content_length": 100},
        ctx,
    )

    assert "B" * 100 + "..." in result.json, "should truncate at custom limit"
    assert "B" * 101 not in result.json, (
        "should not include content past the custom limit"
    )


def test_returns_complete_content_when_full_true_regardless_of_length() -> None:
    long_content = "C" * 2000
    ctx = make_ctx([make_item("i1", long_content)])

    result = handle_cc_get_items(
        {"collection": "test", "item_ids": ["i1"], "full": True}, ctx
    )

    assert "C" * 2000 in result.json, "should contain all 2000 chars"
    assert not result.json.endswith("..."), "should not append ellipsis when full=true"


def test_full_true_overrides_max_content_length() -> None:
    content = "D" * 1000
    ctx = make_ctx([make_item("i1", content)])

    result = handle_cc_get_items(
        {
            "collection": "test",
            "item_ids": ["i1"],
            "full": True,
            "max_content_length": 50,
        },
        ctx,
    )

    assert "D" * 1000 in result.json, (
        "should return full content even with max_content_length set"
    )


def test_content_at_exactly_the_default_limit_is_returned_without_ellipsis() -> None:
    exact_content = "E" * 500
    ctx = make_ctx([make_item("i1", exact_content)])

    result = handle_cc_get_items({"collection": "test", "item_ids": ["i1"]}, ctx)

    assert "E" * 500 in result.json, "should include all 500 chars"
    assert "E" * 500 + "..." not in result.json, (
        "should not append ellipsis at exact limit"
    )


def test_returns_no_items_message_when_item_ids_are_not_found() -> None:
    ctx = make_ctx([])

    result = handle_cc_get_items({"collection": "test", "item_ids": ["missing"]}, ctx)

    assert "No items found" in result.json, "should report no items found"


def test_throws_when_collection_or_item_ids_are_missing() -> None:
    ctx = make_ctx([])

    with pytest.raises(ValueError, match="collection and item_ids are required"):
        handle_cc_get_items({"item_ids": ["i1"]}, ctx)

    with pytest.raises(ValueError, match="collection and item_ids are required"):
        handle_cc_get_items({"collection": "test"}, ctx)


# ── handleCCSaveOverview — persistOverview routing (Feature C) ─────


def test_prefers_ctx_persist_overview_when_present_and_passes_override_through() -> (
    None
):
    calls: list[dict[str, object]] = []
    save_calls: list[dict[str, object]] = []

    def _save(overview: KnowledgeOverview, output_dir: str | None = None) -> str:
        save_calls.append({"overview": overview, "outputDir": output_dir})
        return "/should/not/be/used.json"

    def _persist(
        overview: KnowledgeOverview, output_dir_override: str | None = None
    ) -> str:
        calls.append({"overview": overview, "outputDir": output_dir_override})
        return "/tmp/runs/abc/overviews/persisted--ov1.json"

    ctx = make_ctx([])
    ctx.save_overview = _save
    ctx.persist_overview = _persist

    overview = make_overview(overview_id="ov1", title="Persisted")
    result = handle_cc_save_overview(
        {"overview": overview, "output_dir": "/custom/override"},
        ctx,
    )

    # Exactly one persistOverview call, zero saveOverview calls.
    assert len(calls) == 1, "persistOverview should be called exactly once"
    assert len(save_calls) == 0, (
        "saveOverview should not be called when persistOverview is present"
    )
    recorded = calls[0]["overview"]
    assert isinstance(recorded, KnowledgeOverview)
    assert recorded.overview_id == "ov1"
    assert calls[0]["outputDir"] == "/custom/override"

    # Handler mutated status to 'complete' before delegation.
    assert overview.status == "complete"

    # Response lines: path, ID, title, concepts, themes.
    assert "/tmp/runs/abc/overviews/persisted--ov1.json" in result.json
    assert "ID: ov1" in result.json
    assert "Title: Persisted" in result.json
    assert "Concepts: 0" in result.json
    assert "Themes: 0" in result.json


def test_falls_back_to_ctx_save_overview_when_ctx_persist_overview_is_absent() -> None:
    save_calls: list[dict[str, object]] = []

    def _save(overview: KnowledgeOverview, output_dir: str | None = None) -> str:
        save_calls.append({"overview": overview, "outputDir": output_dir})
        return "/legacy/overviews/fallback--ov2.json"

    ctx = make_ctx([])
    ctx.save_overview = _save
    # persist_overview intentionally omitted (defaults to None).

    overview = make_overview(overview_id="ov2", title="Fallback")
    result = handle_cc_save_overview({"overview": overview}, ctx)

    assert len(save_calls) == 1
    saved = save_calls[0]["overview"]
    assert isinstance(saved, KnowledgeOverview)
    assert saved.overview_id == "ov2"
    assert save_calls[0]["outputDir"] is None
    assert "/legacy/overviews/fallback--ov2.json" in result.json
    assert "ID: ov2" in result.json
    assert "Title: Fallback" in result.json


def test_throws_when_overview_is_missing() -> None:
    ctx = make_ctx([])
    with pytest.raises(ValueError, match="overview is required"):
        handle_cc_save_overview({}, ctx)


# ── collectConcepts — theme template correctness ──────────────────


def _make_research_item(id: str, content: str) -> ResearchItem:
    return ResearchItem(
        id=id, title=f"Item {id}", content=content, tags=[], metadata={}
    )


def test_synthesis_prompt_contains_concept_names_in_theme_instructions() -> None:
    from pipeline_orchestrator.concepts import SourceGroup

    source_groups = [
        SourceGroup(
            "test-collection", [_make_research_item("r1", "Some research content")]
        )
    ]
    result = collect_concepts(source_groups, title="Test Overview")
    assert "concept_names" in result.synthesis_prompt, (
        'synthesis_prompt must mention "concept_names" in theme instructions'
    )


def test_theme_instructions_do_not_use_concepts_as_field_name() -> None:
    import re

    from pipeline_orchestrator.concepts import SourceGroup

    source_groups = [
        SourceGroup(
            "test-collection", [_make_research_item("r1", "Some research content")]
        )
    ]
    result = collect_concepts(source_groups, title="Test Overview")

    themes_block_match = re.search(
        r"For themes:[\s\S]*?(?=\n\n|\n##|$)", result.synthesis_prompt
    )
    assert themes_block_match, 'synthesis_prompt must contain a "For themes:" block'
    themes_block = themes_block_match.group(0)

    assert not re.search(r"[\"']concepts[\"']\s*:", themes_block), (
        'theme instructions must not reference "concepts": as a JSON key'
    )


def test_template_themes_0_has_concept_names_key_and_not_concepts_key() -> None:
    from pipeline_orchestrator.concepts import SourceGroup

    source_groups = [
        SourceGroup(
            "test-collection", [_make_research_item("r1", "Some research content")]
        )
    ]
    result = collect_concepts(source_groups, title="Test Overview")

    assert isinstance(result.template.themes, list), "template.themes must be an array"
    assert len(result.template.themes) > 0, (
        "template.themes must contain at least one placeholder"
    )

    placeholder = result.template.themes[0]
    assert isinstance(placeholder, Theme)
    assert hasattr(placeholder, "concept_names"), (
        'template.themes[0] must have "concept_names" key'
    )
    # The Theme dataclass has no "concepts" attribute by construction.
    assert not hasattr(placeholder, "concepts"), (
        'template.themes[0] must NOT have old "concepts" key'
    )


# ── register_cc_tools — Python-native registration integrity ──────


def test_register_cc_tools_meta_and_annotations() -> None:
    """Integrity: exactly 7 ``pipeline_cc_*`` tools, correct ``_meta`` + annotations.

    Builds a fresh FastMCP, registers the cc tools, and inspects the
    ``mcp.types.Tool`` objects ``list_tools()`` exposes (``.meta`` serializes to
    the wire ``_meta``; ``.annotations`` is the ``ToolAnnotations`` subset).
    ``pipeline_cc_load_collection`` carries only ``_meta`` and no annotations.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pipeline")
    register_cc_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    expected_names = {
        "pipeline_cc_load_collection",
        "pipeline_cc_query",
        "pipeline_cc_get_items",
        "pipeline_cc_collect_concepts",
        "pipeline_cc_save_overview",
        "pipeline_cc_list_overviews",
        "pipeline_cc_get_overview",
    }
    assert set(by_name) == expected_names
    assert len(tools) == 7

    # max_result_chars: 50000 for read/query, 10000 for mutations (§5.4).
    expected_max = {
        "pipeline_cc_load_collection": 10000,
        "pipeline_cc_query": 50000,
        "pipeline_cc_get_items": 50000,
        "pipeline_cc_collect_concepts": 10000,
        "pipeline_cc_save_overview": 10000,
        "pipeline_cc_list_overviews": 50000,
        "pipeline_cc_get_overview": 50000,
    }
    for name, expected in expected_max.items():
        assert by_name[name].meta is not None, f"{name} must carry _meta"
        assert by_name[name].meta == {"max_result_chars": expected}, name

    # load_collection carries ONLY _meta, NO annotations (R1 D1/E1, §4.3).
    assert by_name["pipeline_cc_load_collection"].annotations is None

    # The read-only tools carry readOnlyHint; the mutating tools destructiveHint.
    read_only = (
        "pipeline_cc_query",
        "pipeline_cc_get_items",
        "pipeline_cc_list_overviews",
        "pipeline_cc_get_overview",
    )
    for name in read_only:
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is None, name

    destructive = ("pipeline_cc_collect_concepts", "pipeline_cc_save_overview")
    for name in destructive:
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.destructiveHint is True, name
        assert ann.readOnlyHint is None, name


def test_handle_cc_save_overview_returns_handler_response() -> None:
    """Sanity: the save-overview handler returns a HandlerResponse (str + is_error)."""
    ctx = make_ctx([])
    ctx.save_overview = lambda *a, **k: "/x/y.json"
    overview = make_overview(overview_id="ovx", title="X")
    result = handle_cc_save_overview({"overview": overview}, ctx)
    assert isinstance(result, HandlerResponse)
    assert result.is_error is False


# ── Regression: ensure_ascii=False parity (raw-UTF-8 JSON.stringify) ─


def _ctx_with_real_engine(items: list[ResearchItem]) -> CCContext:
    """A CCContext whose ``collect_concepts`` is the real engine (no BM25)."""
    return CCContext(
        load_collection=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")),
        query_items=lambda *a, **k: [],
        get_items=lambda _collection, ids: [i for i in items if i.id in ids],
        collect_concepts=collect_concepts,
        save_overview=lambda *a, **k: "",
        list_overviews=lambda *a, **k: [],
        get_overview=lambda *a, **k: None,
    )


def test_chunked_response_keeps_raw_dashes_not_unicode_escapes() -> None:
    """The chunked-mode JSON response must mirror Node's raw-UTF-8
    ``JSON.stringify`` — em/en-dashes stay raw, never ``\\uXXXX``-escaped.

    Pre-fix (``json.dumps`` defaulting to ``ensure_ascii=True``) this FAILS:
    the chunk clause ``(items N–M ...)`` and the static prompt em-dashes get
    escaped to ``\\u2013``/``\\u2014``. With ``ensure_ascii=False`` they stay raw.
    Input is ASCII-only, so the dashes can only come from the template/clause.
    """
    # >15 items forces chunk mode; no focus so the BM25 path is skipped.
    items = [make_item(f"item-{i + 1}", f"ascii content {i + 1}") for i in range(20)]
    ctx = _ctx_with_real_engine(items)

    result = asyncio.run(
        handle_cc_collect_concepts(
            {
                "title": "Chunk Parity",
                "items": [
                    {"collection": "col", "item_ids": [it.id for it in items]}
                ],
            },
            ctx,
        )
    )

    resp = result.json
    # Raw UTF-8 dashes present (en-dash in the chunk clause, em-dashes in prompt).
    assert "–" in resp, "raw en-dash should be present in chunked response"
    assert "—" in resp, "raw em-dash should be present in chunked response"
    # The literal Python-escape sequences must NOT leak into the wire string.
    assert "\\u2013" not in resp, "en-dash must not be \\uXXXX-escaped"
    assert "\\u2014" not in resp, "em-dash must not be \\uXXXX-escaped"


def test_get_overview_handler_output_keeps_raw_non_ascii() -> None:
    """The ``handle_cc_get_overview`` JSON output must keep non-ASCII values raw
    (matching Node's ``JSON.stringify(overview, null, 2)``)."""
    overview = make_overview(
        title="Café — Études",
        summary="Naïve façade — résumé",
    )
    ctx = make_ctx([])
    ctx.get_overview = lambda *a, **k: overview

    result = handle_cc_get_overview({"overview_id": "test-id"}, ctx)
    resp = result.json
    assert "é" in resp and "—" in resp, "raw non-ASCII chars should be present"
    assert "\\u00e9" not in resp, "é must not be \\uXXXX-escaped"
    assert "\\u2014" not in resp, "em-dash must not be \\uXXXX-escaped"


# ── handle_cc_query — nullish-not-falsy ``limit`` regression (H1) ───


def _seed_cc_store(
    name: str, items: list[ResearchItem]
) -> Callable[[], None]:
    """Seed the shared module ``STORE`` with a collection and return a teardown.

    Inserts a ``ResearchCollection`` directly (no disk) so ``handle_cc_query``,
    wired to the real ``collections_store.query_items``, sees a non-empty store.
    """
    from pipeline_orchestrator.collections_store import (
        STORE,
        FieldMap,
        ResearchCollection,
    )

    STORE[name] = ResearchCollection(
        name=name,
        file_path=f"/virtual/{name}.json",
        items=items,
        field_map=FieldMap(
            items_key="items",
            id_field="id",
            content_field="content",
            title_field="title",
            tags_field="tags",
        ),
        available_tags=[],
    )

    def _teardown() -> None:
        STORE.pop(name, None)

    return _teardown


def _real_query_ctx() -> CCContext:
    """A CCContext whose ``query_items`` is the REAL shared-store function."""
    from pipeline_orchestrator.collections_store import query_items as real_query_items

    return CCContext(
        load_collection=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")),
        query_items=real_query_items,
        get_items=lambda *a, **k: [],
        collect_concepts=lambda *a, **k: _StubResult(),
        save_overview=lambda *a, **k: "",
        list_overviews=lambda *a, **k: [],
        get_overview=lambda *a, **k: None,
    )


def test_cc_query_omitted_limit_does_not_crash_on_nonempty_store() -> None:
    """Regression for H1: omitting ``limit`` must NOT crash on a non-empty store.

    ``handle_cc_query`` previously forwarded an explicit ``None`` into
    ``query_items(..., limit: int = 20)``, crashing on ``len(results) >= None``.
    After the fix the function default ``20`` applies and all (<=20) items return.
    """
    teardown = _seed_cc_store(
        "cc-limit-col",
        [
            make_item("c1", "alpha"),
            make_item("c2", "beta"),
            make_item("c3", "gamma"),
        ],
    )
    try:
        ctx = _real_query_ctx()
        # Args deliberately OMIT ``limit``.
        result = handle_cc_query({"collection": "cc-limit-col"}, ctx)
        assert result.json.startswith("Found 3 item(s):")
        assert "[c1] Item c1" in result.json
        assert "[c2] Item c2" in result.json
        assert "[c3] Item c3" in result.json
    finally:
        teardown()


def test_cc_query_explicit_limit_is_honored() -> None:
    """An explicit ``limit`` still caps the result count (regression guard)."""
    teardown = _seed_cc_store(
        "cc-explimit-col",
        [
            make_item("d1", "one"),
            make_item("d2", "two"),
            make_item("d3", "three"),
        ],
    )
    try:
        ctx = _real_query_ctx()
        result = handle_cc_query(
            {"collection": "cc-explimit-col", "limit": 1}, ctx
        )
        assert result.json.startswith("Found 1 item(s):")
        assert "[d1] Item d1" in result.json
        assert "[d2] Item d2" not in result.json
    finally:
        teardown()

