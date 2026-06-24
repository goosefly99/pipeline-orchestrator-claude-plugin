"""Port of ``legacy-node/tests/synth-handlers.test.ts`` (7 cases) + additive
integrity tests.

The 7 ported cases exercise ``handle_synth_save_spec`` directly through a mocked
:class:`SynthContext` (the DI seam) — the Feature-C ``persist_spec`` vs
``save_spec`` routing (override pass-through, undefined override, the two
``save_spec`` fallback branches) and the three missing-field guards — with
byte-exact string assertions on the response envelope.

The additive integrity tests mirror the cc precedent (R1 / spec §5.5):

* ``test_register_synth_tools_meta_and_annotations`` — builds a fresh
  ``FastMCP("pipeline")``, registers the synth tools, and asserts via
  ``asyncio.run(mcp.list_tools())`` that exactly the six ``pipeline_synth_*``
  tools register with the spec'd ``_meta.max_result_chars`` (50000 read / 10000
  mutate) and that ``pipeline_synth_load_collection`` carries no annotations
  while the others carry the spec'd ``ToolAnnotations`` subset.
* A shared-store coupling test — a collection loaded via the **cc** path is
  visible to the **synth** ``get_items`` handler through the one module-level
  ``STORE`` (and vice-versa), proving the namespaces are not split.
* A ``synth_get_items`` no-truncation test — the synth handler returns FULL
  content (contrast cc's 500-char truncation).
* A field-map-line parity test — the load-collection field-map line is compact
  ``JSON.stringify(field_map)`` with ``None`` optionals dropped.
* ``ensure_ascii=False`` regression tests — synth handler outputs keep raw
  UTF-8 (em/en-dashes, accented chars) and never ``\\uXXXX``-escape, matching
  Node's raw-UTF-8 ``JSON.stringify`` / template literals.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from pipeline_orchestrator.collections_store import (
    STORE,
    Author,
    FieldMap,
    ResearchCollection,
    ResearchItem,
)
from pipeline_orchestrator.models import DesignSpec
from pipeline_orchestrator.tools.synth_tools import (
    SynthContext,
    handle_synth_get_items,
    handle_synth_load_collection,
    handle_synth_query,
    handle_synth_save_spec,
    register_synth_tools,
)

# ── Helpers (mirror the TS ``makeCtx`` / ``makeSpec`` factories) ───


def _raise(*_a: object, **_k: object) -> object:
    raise RuntimeError("not implemented")


def make_ctx(**overrides: object) -> SynthContext:
    """Mirror the TS ``makeCtx(overrides?)`` — a SynthContext with stubbed callables."""
    base = SynthContext(
        load_collection=_raise,
        query_items=lambda *a, **k: [],
        get_items=lambda *a, **k: [],
        get_specs_dir=lambda: "/default/specs",
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


def make_spec(**overrides: object) -> DesignSpec:
    """Mirror the TS ``makeSpec(overrides?)`` factory."""
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


def make_item(
    *,
    id: str,
    title: str = "",
    content: str = "content",
    tags: list[str] | None = None,
    author: Author | None = None,
    metadata: dict[str, object] | None = None,
    date: str | None = None,
    url: str | None = None,
) -> ResearchItem:
    return ResearchItem(
        id=id,
        title=title,
        content=content,
        tags=tags if tags is not None else [],
        author=author,
        metadata=metadata if metadata is not None else {},
        date=date,
        url=url,
    )


# ── handleSynthSaveSpec — persistSpec routing (Feature C) ──────────


def test_prefers_ctx_persist_spec_when_present_and_passes_override_through() -> None:
    persist_calls: list[dict[str, object]] = []
    get_specs_dir_calls: list[int] = []

    def _get_specs_dir() -> str:
        get_specs_dir_calls.append(1)
        return "/should/not/be/used/specs"

    def _persist(spec: DesignSpec, output_dir_override: str | None = None) -> str:
        persist_calls.append({"spec": spec, "outputDirOverride": output_dir_override})
        return "/tmp/runs/abc/specs/persisted--12345678.json"

    ctx = make_ctx(get_specs_dir=_get_specs_dir, persist_spec=_persist)

    spec = make_spec(
        spec_id="12345678-90ab-cdef-1234-567890abcdef",
        title="Persisted",
        status="approved",
        sources=[
            {"collection": "c1", "item_id": "i1", "title": "Item 1", "relevance": ""},
            {"collection": "c1", "item_id": "i2", "title": "Item 2", "relevance": ""},
        ],
    )

    result = handle_synth_save_spec(
        {"spec": spec, "output_dir": "/custom/override"}, ctx
    )

    # Exactly one persist_spec call, zero get_specs_dir calls.
    assert len(persist_calls) == 1, "persist_spec should be called exactly once"
    assert len(get_specs_dir_calls) == 0, (
        "get_specs_dir should not be consulted when persist_spec is present"
    )
    recorded = persist_calls[0]["spec"]
    assert isinstance(recorded, DesignSpec)
    assert recorded.spec_id == "12345678-90ab-cdef-1234-567890abcdef"
    assert persist_calls[0]["outputDirOverride"] == "/custom/override"

    # Byte-identical response envelope.
    assert result.json == (
        "Spec saved: /tmp/runs/abc/specs/persisted--12345678.json\n"
        "  Title: Persisted\n"
        "  Status: approved\n"
        "  Sources: 2"
    )


def test_prefers_ctx_persist_spec_with_undefined_override_when_no_output_dir() -> None:
    persist_calls: list[dict[str, object]] = []

    def _persist(spec: DesignSpec, output_dir_override: str | None = None) -> str:
        persist_calls.append({"spec": spec, "outputDirOverride": output_dir_override})
        return "/tmp/runs/def/specs/noarg--abcdef12.json"

    ctx = make_ctx(persist_spec=_persist)

    spec = make_spec(spec_id="abcdef12-90ab-cdef-1234-567890abcdef", title="NoArg")

    result = handle_synth_save_spec({"spec": spec}, ctx)

    assert len(persist_calls) == 1
    assert persist_calls[0]["outputDirOverride"] is None
    assert result.json.startswith(
        "Spec saved: /tmp/runs/def/specs/noarg--abcdef12.json"
    )


def test_falls_back_to_module_save_spec_using_get_specs_dir(tmp_path: Path) -> None:
    # When ctx.persist_spec is absent, handle_synth_save_spec delegates to the
    # module-level save_spec(spec, output_dir ?? ctx.get_specs_dir()). This test
    # exercises the fallback branch end-to-end on disk via a hermetic temp dir.
    get_specs_dir_calls: list[int] = []

    def _get_specs_dir() -> str:
        get_specs_dir_calls.append(1)
        return str(tmp_path)

    ctx = make_ctx(get_specs_dir=_get_specs_dir)
    # persist_spec intentionally omitted (defaults to None).

    spec = make_spec(
        spec_id="fa11bac0-90ab-cdef-1234-567890abcdef",
        title="Fallback",
        status="draft",
    )

    result = handle_synth_save_spec({"spec": spec}, ctx)

    assert len(get_specs_dir_calls) == 1
    expected_file = os.path.join(str(tmp_path), "fallback--fa11bac0.json")
    assert os.path.exists(expected_file), f"expected file at {expected_file}"
    assert result.json == (
        f"Spec saved: {expected_file}\n"
        "  Title: Fallback\n"
        "  Status: draft\n"
        "  Sources: 0"
    )


def test_falls_back_to_module_save_spec_with_explicit_output_dir_arg(
    tmp_path: Path,
) -> None:
    get_specs_dir_calls: list[int] = []

    def _get_specs_dir() -> str:
        get_specs_dir_calls.append(1)
        return "/should/not/be/used"

    ctx = make_ctx(get_specs_dir=_get_specs_dir)
    # persist_spec intentionally omitted.

    spec = make_spec(spec_id="0ver1de0-90ab-cdef-1234-567890abcdef", title="Overridden")

    result = handle_synth_save_spec({"spec": spec, "output_dir": str(tmp_path)}, ctx)

    # When output_dir is supplied, get_specs_dir should not be consulted.
    assert len(get_specs_dir_calls) == 0
    expected_file = os.path.join(str(tmp_path), "overridden--0ver1de0.json")
    assert os.path.exists(expected_file)
    assert result.json.startswith(f"Spec saved: {expected_file}")


def test_throws_when_spec_is_missing() -> None:
    ctx = make_ctx()
    with pytest.raises(ValueError, match="spec is required"):
        handle_synth_save_spec({}, ctx)


def test_throws_when_spec_is_missing_spec_id() -> None:
    ctx = make_ctx()
    spec = make_spec()
    spec.spec_id = ""  # deliberately invalid (empty falsy id)
    with pytest.raises(ValueError, match="spec must have spec_id and title"):
        handle_synth_save_spec({"spec": spec}, ctx)


def test_throws_when_spec_is_missing_title() -> None:
    ctx = make_ctx()
    spec = make_spec()
    spec.title = ""  # deliberately invalid (empty falsy title)
    with pytest.raises(ValueError, match="spec must have spec_id and title"):
        handle_synth_save_spec({"spec": spec}, ctx)


# ── register_synth_tools — Python-native registration integrity ────


def test_register_synth_tools_meta_and_annotations() -> None:
    """Integrity: exactly 6 ``pipeline_synth_*`` tools, correct ``_meta`` + annotations.

    Builds a fresh FastMCP, registers the synth tools, and inspects the
    ``mcp.types.Tool`` objects ``list_tools()`` exposes.
    ``pipeline_synth_load_collection`` carries only ``_meta`` and no annotations.
    ``register_synth_tools`` is NOT wired into the global ``register_tools`` seam
    — this test constructs its own FastMCP instance, so the global handshake
    stays at 0.
    """
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pipeline")
    register_synth_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    expected_names = {
        "pipeline_synth_load_collection",
        "pipeline_synth_query",
        "pipeline_synth_get_items",
        "pipeline_synth_create_spec",
        "pipeline_synth_save_spec",
        "pipeline_synth_list_specs",
    }
    assert set(by_name) == expected_names
    assert len(tools) == 6

    # max_result_chars: 50000 for read, 10000 for mutations (§5.5).
    expected_max = {
        "pipeline_synth_load_collection": 10000,
        "pipeline_synth_query": 50000,
        "pipeline_synth_get_items": 50000,
        "pipeline_synth_create_spec": 10000,
        "pipeline_synth_save_spec": 10000,
        "pipeline_synth_list_specs": 50000,
    }
    for name, expected in expected_max.items():
        assert by_name[name].meta is not None, f"{name} must carry _meta"
        assert by_name[name].meta == {"max_result_chars": expected}, name

    # load_collection carries ONLY _meta, NO annotations (§5.5).
    assert by_name["pipeline_synth_load_collection"].annotations is None

    read_only = (
        "pipeline_synth_query",
        "pipeline_synth_get_items",
        "pipeline_synth_list_specs",
    )
    for name in read_only:
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is None, name

    destructive = ("pipeline_synth_create_spec", "pipeline_synth_save_spec")
    for name in destructive:
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.destructiveHint is True, name
        assert ann.readOnlyHint is None, name


# ── Shared-store coupling (cc ⇄ synth read/write the SAME STORE) ───


def test_synth_get_items_sees_collection_in_shared_store() -> None:
    """A collection placed directly in the module-level ``STORE`` is visible to
    the synth ``get_items`` handler — proving cc and synth share one namespace.
    """
    from pipeline_orchestrator.collections_store import get_items as real_get_items

    col = ResearchCollection(
        name="shared-col",
        file_path="/virtual/shared-col.json",
        items=[
            make_item(id="x1", title="Shared One", content="alpha"),
            make_item(id="x2", title="Shared Two", content="beta"),
        ],
        field_map=FieldMap(
            items_key="items",
            id_field="id",
            content_field="content",
            title_field="title",
            tags_field="tags",
        ),
        available_tags=[],
    )
    STORE["shared-col"] = col
    try:
        # ctx.get_items is wired to the SAME shared-store function the cc domain
        # uses; the synth handler reads through it.
        ctx = make_ctx(get_items=real_get_items)
        result = handle_synth_get_items(
            {"collection": "shared-col", "item_ids": ["x1", "x2"]}, ctx
        )
        assert "Shared One" in result.json
        assert "Shared Two" in result.json
        assert "alpha" in result.json and "beta" in result.json
    finally:
        STORE.pop("shared-col", None)


# ── synth_get_items — FULL content, NO truncation ─────────────────


def test_synth_get_items_returns_full_content_without_truncation() -> None:
    """Contrast with cc_get_items (500-char truncate): synth returns full content."""
    long_content = "Z" * 2000
    item = make_item(id="big", title="Big Item", content=long_content)
    ctx = make_ctx(get_items=lambda *a, **k: [item])

    result = handle_synth_get_items(
        {"collection": "any", "item_ids": ["big"]}, ctx
    )

    assert "Z" * 2000 in result.json, "synth must return all 2000 chars (no truncation)"
    assert "..." not in result.json, "synth must not append an ellipsis"


def test_synth_get_items_emits_all_metadata_with_object_values_as_compact_json() -> (
    None
):
    """Metadata object values are compact ``JSON.stringify(v)``; primitives
    via String()."""
    item = make_item(
        id="m1",
        title="Meta Item",
        content="body",
        metadata={"platform": "x", "deps": ["a", "b"], "engagement": 42},
    )
    ctx = make_ctx(get_items=lambda *a, **k: [item])

    result = handle_synth_get_items(
        {"collection": "any", "item_ids": ["m1"]}, ctx
    )

    assert "platform: x" in result.json
    assert 'deps: ["a","b"]' in result.json, "array metadata must be compact JSON"
    assert "engagement: 42" in result.json


# ── synth_load_collection — field-map line parity ─────────────────


def test_load_collection_field_map_line_is_compact_json_dropping_none_optionals() -> (
    None
):
    """The field-map line mirrors ``JSON.stringify(field_map)``: compact, ``None``
    optionals omitted, keys in interface order.
    """
    col = ResearchCollection(
        name="fmcol",
        file_path="/virtual/fmcol.json",
        items=[make_item(id="a", title="A", tags=["t1", "t2"])],
        field_map=FieldMap(
            items_key="items",
            id_field="id",
            content_field="content",
            title_field="title",
            tags_field="tags",
            author_field="author",
            # date_field / url_field left None — must be dropped.
        ),
        available_tags=["t1", "t2"],
    )

    ctx = make_ctx(load_collection=lambda *a, **k: col)
    result = handle_synth_load_collection({"file_path": "/virtual/fmcol.json"}, ctx)

    assert (
        '  Field map: {"items_key":"items","id_field":"id",'
        '"content_field":"content","title_field":"title",'
        '"tags_field":"tags","author_field":"author"}'
    ) in result.json
    # The None optionals must not leak into the line.
    assert "date_field" not in result.json
    assert "url_field" not in result.json
    # First-5-items block renders the tag preview.
    assert "[a] A (t1, t2)" in result.json


def test_load_collection_passes_flat_overrides_through() -> None:
    """The five flat string overrides are forwarded as a partial field-map dict."""
    captured: dict[str, object] = {}

    def _load(
        file_path: object, name: object = None, overrides: object = None
    ) -> object:
        captured["file_path"] = file_path
        captured["name"] = name
        captured["overrides"] = overrides
        return ResearchCollection(
            name="ov",
            file_path="/virtual/ov.json",
            items=[],
            field_map=FieldMap(
                items_key="records",
                id_field="uid",
                content_field="body",
                title_field="headline",
                tags_field="labels",
            ),
            available_tags=[],
        )

    ctx = make_ctx(load_collection=_load)
    handle_synth_load_collection(
        {
            "file_path": "/virtual/ov.json",
            "name": "ov",
            "items_key": "records",
            "id_field": "uid",
            "content_field": "body",
            "title_field": "headline",
            "tags_field": "labels",
        },
        ctx,
    )

    assert captured["overrides"] == {
        "items_key": "records",
        "id_field": "uid",
        "content_field": "body",
        "title_field": "headline",
        "tags_field": "labels",
    }


# ── ensure_ascii=False parity (raw-UTF-8 JSON.stringify / templates) ─


def test_save_spec_response_keeps_raw_non_ascii() -> None:
    """The save-spec response envelope keeps raw non-ASCII (em-dash, accents)."""
    captured_path = "/tmp/specs/café—études--12345678.json"
    ctx = make_ctx(persist_spec=lambda *a, **k: captured_path)
    spec = make_spec(spec_id="12345678-aaaa", title="Café — Études")

    result = handle_synth_save_spec({"spec": spec}, ctx)
    resp = result.json
    assert "Café — Études" in resp, "raw accented chars + em-dash must be present"
    assert "\\u00e9" not in resp, "é must not be \\uXXXX-escaped"
    assert "\\u2014" not in resp, "em-dash must not be \\uXXXX-escaped"


def test_get_items_metadata_object_keeps_raw_non_ascii() -> None:
    """An object metadata value with non-ASCII content stays raw in the
    JSON.stringify."""
    item = make_item(
        id="u1",
        title="Unicode",
        content="body",
        metadata={"locales": ["café", "naïve"]},
    )
    ctx = make_ctx(get_items=lambda *a, **k: [item])

    result = handle_synth_get_items({"collection": "any", "item_ids": ["u1"]}, ctx)
    resp = result.json
    assert '["café","naïve"]' in resp, "array metadata must keep raw non-ASCII"
    assert "\\u00e9" not in resp, "é must not be \\uXXXX-escaped"


def test_create_spec_prompt_embeds_template_with_raw_dashes() -> None:
    """The embedded JSON template + prompt headers keep raw UTF-8 (no \\uXXXX)."""
    from pipeline_orchestrator.synth import create_spec_synthesis

    item = make_item(id="s1", title="Strat — One", content="ascii body", tags=["t"])
    _template, prompt = create_spec_synthesis(
        "Spec — Title",
        [{"collection": "col", "items": [item], "relevance_notes": {}}],
    )
    assert "Spec — Title" in prompt, "raw em-dash in the H1 header"
    assert "\\u2014" not in prompt, "em-dash must not be \\uXXXX-escaped in the prompt"
    # The blank-template JSON block is embedded verbatim.
    assert "```json" in prompt
    assert '"spec_type": "implementation"' in prompt


# ── handle_synth_query — nullish-not-falsy ``limit`` regression (H1) ─


def test_synth_query_omitted_limit_does_not_crash_on_nonempty_store() -> None:
    """Regression for H1: omitting ``limit`` must NOT crash on a non-empty store.

    Wires ``ctx.query_items`` to the REAL ``collections_store.query_items`` and
    seeds the shared ``STORE`` with a >1-item collection. Calling
    ``handle_synth_query`` WITHOUT a ``limit`` arg previously forwarded an
    explicit ``None`` into ``query_items(..., limit: int = 20)``, crashing on
    ``len(results) >= None``. After the fix the function's default ``20`` applies
    and all (<=20) items are returned.
    """
    from pipeline_orchestrator.collections_store import query_items as real_query_items

    col = ResearchCollection(
        name="limit-col",
        file_path="/virtual/limit-col.json",
        items=[
            make_item(id="q1", title="First", content="alpha", tags=["x"]),
            make_item(id="q2", title="Second", content="beta", tags=["y"]),
            make_item(id="q3", title="Third", content="gamma", tags=["z"]),
        ],
        field_map=FieldMap(
            items_key="items",
            id_field="id",
            content_field="content",
            title_field="title",
            tags_field="tags",
        ),
        available_tags=["x", "y", "z"],
    )
    STORE["limit-col"] = col
    try:
        ctx = make_ctx(query_items=real_query_items)
        # Args deliberately OMIT ``limit``.
        result = handle_synth_query({"collection": "limit-col"}, ctx)
        assert result.json.startswith("Found 3 item(s):")
        assert "[q1] First" in result.json
        assert "[q2] Second" in result.json
        assert "[q3] Third" in result.json
    finally:
        STORE.pop("limit-col", None)


def test_synth_query_explicit_limit_is_honored() -> None:
    """An explicit ``limit`` still caps the result count (regression guard)."""
    from pipeline_orchestrator.collections_store import query_items as real_query_items

    col = ResearchCollection(
        name="explimit-col",
        file_path="/virtual/explimit-col.json",
        items=[
            make_item(id="e1", title="One", content="a"),
            make_item(id="e2", title="Two", content="b"),
            make_item(id="e3", title="Three", content="c"),
        ],
        field_map=FieldMap(
            items_key="items",
            id_field="id",
            content_field="content",
            title_field="title",
            tags_field="tags",
        ),
        available_tags=[],
    )
    STORE["explimit-col"] = col
    try:
        ctx = make_ctx(query_items=real_query_items)
        result = handle_synth_query(
            {"collection": "explimit-col", "limit": 1}, ctx
        )
        assert result.json.startswith("Found 1 item(s):")
        assert "[e1] One" in result.json
        assert "[e2] Two" not in result.json
    finally:
        STORE.pop("explimit-col", None)


def test_synth_query_explicit_limit_zero_is_forwarded_not_defaulted() -> None:
    """Nullish-not-falsy: an explicit ``limit=0`` is FORWARDED, never coerced to 20.

    Matches the JS ``{ limit = 20 }`` semantic — ``0`` is a real value, only
    ``undefined`` triggers the default. The shared ``query_items`` loop pushes an
    item BEFORE the ``len(results) >= limit`` break (identical to the Node oracle
    ``collections.ts:189``: ``results.push(item); if (results.length >= limit)
    break``), so ``limit=0`` returns exactly ONE item — decisively fewer than the
    three a defaulted ``limit=20`` would return. This proves the handler did not
    silently default the falsy ``0``.
    """
    from pipeline_orchestrator.collections_store import query_items as real_query_items

    col = ResearchCollection(
        name="zerolimit-col",
        file_path="/virtual/zerolimit-col.json",
        items=[
            make_item(id="z1", title="Alpha", content="a"),
            make_item(id="z2", title="Beta", content="b"),
            make_item(id="z3", title="Gamma", content="c"),
        ],
        field_map=FieldMap(
            items_key="items",
            id_field="id",
            content_field="content",
            title_field="title",
            tags_field="tags",
        ),
        available_tags=[],
    )
    STORE["zerolimit-col"] = col
    try:
        ctx = make_ctx(query_items=real_query_items)
        result = handle_synth_query(
            {"collection": "zerolimit-col", "limit": 0}, ctx
        )
        # limit=0 forwarded -> exactly one item (oracle parity), NOT three.
        assert result.json.startswith("Found 1 item(s):")
        assert "[z1] Alpha" in result.json
        assert "[z2] Beta" not in result.json
        assert "[z3] Gamma" not in result.json
    finally:
        STORE.pop("zerolimit-col", None)

