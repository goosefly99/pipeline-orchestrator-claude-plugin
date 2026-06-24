"""Focused tests for the four KB MCP-tool handlers (``kb_tools.py``).

There is **no** dedicated Node test file for these handlers, so these are
author-written FOCUSED tests (additive — **0 NEW parity cases**; the 834-parity
count is unchanged). They pin the contracts that the byte-exact port must hold:
the frozen ``handle_kb_search`` merge response shape + key order, the additive
env-gated vector leg (disabled / ok / non-ok passthrough), the BM25 build
shape, the SQL-template passthrough, the query-log export (incl. raw non-ASCII /
``ensure_ascii=False`` regression), every required-arg guard, and the
``register_kb_tools`` enumeration integrity.

House idioms mirrored from the siblings (``test_cc_handlers.py`` /
``test_kb_client.py``): async handlers driven with ``asyncio.run(...)`` (no
pytest-asyncio); the kb_client module stores reset between tests via
``clear_query_log()`` + an adapter-registry teardown (cf. ``test_kb_client``).
``register_kb_tools`` builds its own ``FastMCP`` instance, so the global
``register_tools`` handshake is untouched.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Literal, cast

import pytest

import pipeline_orchestrator.kb_client as kb_client
from pipeline_orchestrator.bm25 import BM25Index, bm25_index_dir
from pipeline_orchestrator.collections_store import (
    STORE,
    FieldMap,
    ResearchCollection,
    ResearchItem,
)
from pipeline_orchestrator.kb_client import (
    KBClient,
    KBProviderAdapter,
    VectorSearchParams,
    VectorSearchResult,
    clear_query_log,
    get_query_log,
)
from pipeline_orchestrator.models import HandlerResponse, RunState
from pipeline_orchestrator.tools.kb_tools import (
    KBContext,
    handle_kb_build_index,
    handle_kb_export_query_log,
    handle_kb_search,
    handle_kb_sql_query,
    register_kb_tools,
)

# ── Shared store / kb-client reset ────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state() -> object:
    """Reset the kb_client in-memory stores + adapter registry and the shared
    collection STORE, and ensure the vector env gate is unset, around each test
    (mirrors the ``test_kb_client`` ``clear_query_log`` discipline)."""
    clear_query_log()
    kb_client._adapter_registry.clear()
    STORE.clear()
    os.environ.pop("SECOND_BRAIN_INDEX_PATH", None)
    yield
    clear_query_log()
    kb_client._adapter_registry.clear()
    STORE.clear()
    os.environ.pop("SECOND_BRAIN_INDEX_PATH", None)


# ── Helpers ───────────────────────────────────────────────────────


def make_item(
    id: str,
    content: str,
    title: str = "",
    tags: list[str] | None = None,
) -> ResearchItem:
    return ResearchItem(
        id=id,
        title=title or f"Item {id}",
        content=content,
        tags=tags or [],
        metadata={},
    )


def _sentinel_run() -> RunState:
    """A minimal truthy ``RunState`` for the active-run base-dir branch."""
    return RunState(
        run_id="r",
        pipeline_version="1.0.0",
        created_at="2026-01-01T00:00:00.000Z",
        updated_at="2026-01-01T00:00:00.000Z",
        status="running",
        config_path="/virtual/pipeline.toml",
    )


def make_ctx(base_dir: str, active: bool = False, run_dir: str = "") -> KBContext:
    """A KBContext that resolves ``base_dir`` either via the active-run path or
    the project-root/storage-base path, depending on ``active``."""

    def _base_from_run(run_dir_arg: str) -> str:
        return base_dir

    if active:
        run = _sentinel_run()
        return KBContext(
            get_project_root=lambda: "/unused/project",
            get_config_storage_base_dir=lambda: ".unused",
            get_active_run=lambda: run,  # truthy
            get_active_run_dir=lambda: run_dir or "/runs/active",
            base_dir_from_run_dir=_base_from_run,
        )
    # Non-active: base_dir == abspath(join(project_root, storage_base)). Split
    # base_dir into project_root + a "." storage base so the join resolves back.
    return KBContext(
        get_project_root=lambda: base_dir,
        get_config_storage_base_dir=lambda: ".",
        get_active_run=lambda: None,
        get_active_run_dir=lambda: "/should/not/be/used",
        base_dir_from_run_dir=lambda _d: "/should/not/be/used",
    )


def _seed_collection(name: str, items: list[ResearchItem]) -> None:
    """Insert a collection directly into the shared STORE (no disk)."""
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


def _build_and_persist(
    base_dir: str, collection: str, items: list[ResearchItem]
) -> None:
    """Build + persist a real BM25 index for ``collection`` under ``base_dir``."""
    idx = BM25Index()
    idx.build(items, collection)
    idx.persist(bm25_index_dir(base_dir, collection))


# ── Fake vector adapter (the vector:second_brain adapter is registered in
#    server.py at T6.1; tests register their own so the leg is exercised) ──


class _FakeVectorAdapter:
    """A minimal ``vector:second_brain`` adapter for the env-set vector-leg
    tests (the real one is wired in server.py at T6.1).

    Implements only ``vector_search`` (like the real ``SecondBrainAdapter``);
    it is ``cast`` to ``KBProviderAdapter`` at the ``register_adapter`` call
    sites since the Protocol's ``sql_query`` member is unused on this path.
    """

    type: Literal["vector"] = "vector"
    provider = "second_brain"

    def __init__(self, result: VectorSearchResult) -> None:
        self._result = result
        self.calls: list[VectorSearchParams] = []

    async def vector_search(
        self, client: KBClient, params: VectorSearchParams
    ) -> VectorSearchResult:
        self.calls.append(params)
        return self._result


# ── handle_kb_search — no-index error ─────────────────────────────


def test_kb_search_no_index_error_shape(tmp_path: object) -> None:
    ctx = make_ctx(str(tmp_path))  # kb/bm25/ does not exist
    result = asyncio.run(
        handle_kb_search({"run_id": "r1", "phase": "p1", "query": "q"}, ctx)
    )
    assert isinstance(result, HandlerResponse)
    assert result.is_error is True
    payload = json.loads(result.json)
    assert payload == {
        "status": "error",
        "results": [],
        "results_count": 0,
        "error": (
            "No BM25 indexes found. Build one using pipeline_kb_build_index "
            "first."
        ),
    }
    # No query is logged on the no-index path.
    assert get_query_log("r1", "p1") == []


# ── handle_kb_search — happy BM25 path: merge / sort / slice + log + shape ─


def test_kb_search_happy_merge_sort_slice_and_log(tmp_path: object) -> None:
    base_dir = str(tmp_path)
    # Two collections, several items; "alpha" appears with varying strength.
    col_a = [
        make_item("a1", "alpha alpha alpha beta", title="A1"),
        make_item("a2", "alpha gamma", title="A2"),
        make_item("a3", "nothing relevant here", title="A3"),
    ]
    col_b = [
        make_item("b1", "alpha alpha delta", title="B1"),
        make_item("b2", "alpha", title="B2"),
    ]
    _build_and_persist(base_dir, "col_a", col_a)
    _build_and_persist(base_dir, "col_b", col_b)

    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search(
            {"run_id": "rid", "phase": "ph", "query": "alpha", "top_k": 3}, ctx
        )
    )
    assert result.is_error is False
    payload = json.loads(result.json)

    # Frozen top-level key order.
    assert list(payload.keys()) == [
        "status",
        "results",
        "results_count",
        "vector_results",
        "vector_status",
    ]
    assert payload["status"] == "ok"
    # slice top_k=3 applied across the merge.
    assert payload["results_count"] == 3
    assert len(payload["results"]) == 3

    # Each merged hit serializes to the frozen key order.
    for hit in payload["results"]:
        assert list(hit.keys()) == ["item_id", "score", "metadata", "collection"]
        assert list(hit["metadata"].keys()) == ["title", "tags"]

    # Sorted by score desc.
    scores = [h["score"] for h in payload["results"]]
    assert scores == sorted(scores, reverse=True)
    # a1 (3x alpha) is the strongest hit.
    assert payload["results"][0]["item_id"] == "a1"
    assert payload["results"][0]["collection"] == "col_a"

    # Vector leg disabled (env unset by the fixture).
    assert payload["vector_status"] == "disabled"
    assert payload["vector_results"] == []

    # The query was logged with the (sliced) result count.
    log = get_query_log("rid", "ph")
    assert len(log) == 1
    assert log[0]["source"] == "bm25"
    assert log[0]["query"] == "alpha"
    assert log[0]["results_count"] == 3


# ── handle_kb_search — float top_k (H1 regression) ────────────────
#
# The tool schema declares ``top_k: number`` so a conformant client may send
# a float (e.g. ``3.0``). Node ``(args.top_k ?? 5)`` then ``.slice(0, topK)``
# truncates toward zero (``3.0``→3, ``2.7``→2). The Python handler must do
# the same instead of crashing on the float (the old ``assert isinstance(
# top_k, int)`` raised AssertionError; ``list[:3.0]`` raises TypeError).


@pytest.mark.parametrize(
    ("float_top_k", "expected_count"),
    [(3.0, 3), (2.7, 2)],
)
def test_kb_search_float_top_k_truncates_toward_zero(
    tmp_path: object, float_top_k: float, expected_count: int
) -> None:
    """A float ``top_k`` truncates toward zero (JS slice parity) instead of
    crashing. ``3.0``→3 and ``2.7``→2, matching Node ``.slice(0, top_k)``."""
    base_dir = str(tmp_path)
    # >= 3 items all matching "alpha" so the slice — not a shortage of hits —
    # is what bounds the result count.
    items = [
        make_item("i1", "alpha alpha alpha", title="I1"),
        make_item("i2", "alpha alpha", title="I2"),
        make_item("i3", "alpha", title="I3"),
        make_item("i4", "alpha beta", title="I4"),
    ]
    _build_and_persist(base_dir, "c", items)

    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search(
            {
                "run_id": "rid",
                "phase": "ph",
                "query": "alpha",
                "top_k": float_top_k,
            },
            ctx,
        )
    )
    assert result.is_error is False
    payload = json.loads(result.json)
    # The float was truncated toward zero — no crash, correct count.
    assert payload["results_count"] == expected_count
    assert len(payload["results"]) == expected_count
    # The logged count reflects the (truncated) slice too.
    log = get_query_log("rid", "ph")
    assert log[0]["results_count"] == expected_count


def test_kb_search_active_run_base_dir_resolution(tmp_path: object) -> None:
    """When an active run is present, base_dir is resolved via
    ``base_dir_from_run_dir(get_active_run_dir())``."""
    base_dir = str(tmp_path)
    _build_and_persist(base_dir, "c", [make_item("x1", "alpha alpha", title="X1")])
    ctx = make_ctx(base_dir, active=True, run_dir="/runs/abc")
    result = asyncio.run(
        handle_kb_search({"run_id": "r", "phase": "p", "query": "alpha"}, ctx)
    )
    payload = json.loads(result.json)
    assert payload["results_count"] == 1
    assert payload["results"][0]["item_id"] == "x1"


def test_kb_search_filter_tags_bridge(tmp_path: object) -> None:
    """``filter.tags`` is bridged to ``BM25Index.search(filter_tags=...)``."""
    base_dir = str(tmp_path)
    items = [
        make_item("t1", "alpha content", title="T1", tags=["crypto"]),
        make_item("t2", "alpha content", title="T2", tags=["sports"]),
    ]
    _build_and_persist(base_dir, "tagged", items)
    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search(
            {
                "run_id": "r",
                "phase": "p",
                "query": "alpha",
                "filter": {"tags": ["crypto"]},
            },
            ctx,
        )
    )
    payload = json.loads(result.json)
    ids = [h["item_id"] for h in payload["results"]]
    assert ids == ["t1"], "only the crypto-tagged item should pass the filter"


# ── handle_kb_search — vector leg ─────────────────────────────────


def test_kb_search_vector_leg_disabled_no_adapter_call(tmp_path: object) -> None:
    """Env unset → vector_status='disabled', no adapter call, vector_results=[]."""
    base_dir = str(tmp_path)
    _build_and_persist(base_dir, "c", [make_item("i1", "alpha", title="I1")])
    adapter = _FakeVectorAdapter(
        {"status": "ok", "results": [{"id": "v1"}], "results_count": 1}
    )
    kb_client.register_adapter(
        cast(KBProviderAdapter, adapter)
    )  # registered but env unset → not called
    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search({"run_id": "r", "phase": "p", "query": "alpha"}, ctx)
    )
    payload = json.loads(result.json)
    assert payload["vector_status"] == "disabled"
    assert payload["vector_results"] == []
    assert adapter.calls == [], "adapter must NOT be called when env unset"


def test_kb_search_vector_leg_env_set_ok_merges_vector_results(
    tmp_path: object,
) -> None:
    """Env set + a registered ok adapter → vector_status='ok' + vector_results
    populated, while BM25 results/results_count are untouched."""
    base_dir = str(tmp_path)
    _build_and_persist(base_dir, "c", [make_item("i1", "alpha alpha", title="I1")])
    vec_payload: list[object] = [{"id": "v1", "snippet": "café"}, {"id": "v2"}]
    adapter = _FakeVectorAdapter(
        {"status": "ok", "results": vec_payload, "results_count": 2}
    )
    kb_client.register_adapter(cast(KBProviderAdapter, adapter))
    os.environ["SECOND_BRAIN_INDEX_PATH"] = "/some/index"
    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search(
            {
                "run_id": "r",
                "phase": "p",
                "query": "alpha",
                "filter": {"tags": ["x"]},
            },
            ctx,
        )
    )
    payload = json.loads(result.json)
    # BM25 leg unaffected.
    assert payload["status"] == "ok"
    # (filter tags ["x"] don't match the untagged item, so BM25 results empty —
    # this proves the vector leg never mutates results/results_count.)
    assert payload["results"] == []
    assert payload["results_count"] == 0
    # Vector leg merged in.
    assert payload["vector_status"] == "ok"
    assert payload["vector_results"] == vec_payload
    # The adapter was called with the filter passed through.
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["filter"] == {"tags": ["x"]}
    # Raw non-ASCII preserved (ensure_ascii=False).
    assert "café" in result.json
    assert "\\u00e9" not in result.json


def test_kb_search_vector_leg_env_set_non_ok_status_passthrough(
    tmp_path: object,
) -> None:
    """Env set + a non-ok adapter result → vector_results=[] and
    vector_status=result['status'] (passthrough)."""
    base_dir = str(tmp_path)
    _build_and_persist(base_dir, "c", [make_item("i1", "alpha", title="I1")])
    adapter = _FakeVectorAdapter(
        {
            "status": "provider_not_configured",
            "results": [{"id": "ignored"}],
            "results_count": 1,
            "error": "nope",
        }
    )
    kb_client.register_adapter(cast(KBProviderAdapter, adapter))
    os.environ["SECOND_BRAIN_INDEX_PATH"] = "/some/index"
    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search({"run_id": "r", "phase": "p", "query": "alpha"}, ctx)
    )
    payload = json.loads(result.json)
    assert payload["vector_status"] == "provider_not_configured"
    assert payload["vector_results"] == []


def test_kb_search_vector_leg_env_set_no_adapter_stub(tmp_path: object) -> None:
    """Env set but NO adapter registered → the kb_client provider stub fires;
    status is 'provider_not_configured' (no isolation crash)."""
    base_dir = str(tmp_path)
    _build_and_persist(base_dir, "c", [make_item("i1", "alpha", title="I1")])
    os.environ["SECOND_BRAIN_INDEX_PATH"] = "/some/index"
    ctx = make_ctx(base_dir)
    result = asyncio.run(
        handle_kb_search({"run_id": "r", "phase": "p", "query": "alpha"}, ctx)
    )
    payload = json.loads(result.json)
    assert payload["vector_status"] == "provider_not_configured"
    assert payload["vector_results"] == []


# ── handle_kb_search — required-arg guards ────────────────────────


def test_kb_search_required_arg_guards(tmp_path: object) -> None:
    ctx = make_ctx(str(tmp_path))
    with pytest.raises(ValueError, match="run_id is required"):
        asyncio.run(handle_kb_search({"phase": "p", "query": "q"}, ctx))
    with pytest.raises(ValueError, match="phase is required"):
        asyncio.run(handle_kb_search({"run_id": "r", "query": "q"}, ctx))
    with pytest.raises(ValueError, match="query is required"):
        asyncio.run(handle_kb_search({"run_id": "r", "phase": "p"}, ctx))


# ── handle_kb_sql_query ───────────────────────────────────────────


_SQL_CFG: dict[str, object] = {
    "name": "market-db",
    "type": "sql",
    "provider": "postgres",
    "config": {"host": "localhost"},
}


def test_kb_sql_query_required_arg_guards() -> None:
    with pytest.raises(ValueError, match="run_id is required"):
        asyncio.run(
            handle_kb_sql_query(
                {"phase": "p", "template_name": "t", "kb_config": _SQL_CFG}
            )
        )
    with pytest.raises(ValueError, match="phase is required"):
        asyncio.run(
            handle_kb_sql_query(
                {"run_id": "r", "template_name": "t", "kb_config": _SQL_CFG}
            )
        )
    with pytest.raises(ValueError, match="template_name is required"):
        asyncio.run(
            handle_kb_sql_query({"run_id": "r", "phase": "p", "kb_config": _SQL_CFG})
        )
    with pytest.raises(ValueError, match="kb_config is required"):
        asyncio.run(
            handle_kb_sql_query(
                {"run_id": "r", "phase": "p", "template_name": "t"}
            )
        )


def test_kb_sql_query_stub_passthrough_and_logs() -> None:
    """With no adapter registered the kb_client SQL stub fires
    (provider_not_configured); the result passes through, is_error False, and
    the query is logged with default empty parameters."""
    result = asyncio.run(
        handle_kb_sql_query(
            {
                "run_id": "sr",
                "phase": "sp",
                "template_name": "get_history",
                "kb_config": _SQL_CFG,
            }
        )
    )
    assert result.is_error is False
    payload = json.loads(result.json)
    assert payload["status"] == "provider_not_configured"
    log = get_query_log("sr", "sp")
    assert len(log) == 1
    assert log[0]["query"] == "get_history"
    assert log[0]["parameters"] == {}


def test_kb_sql_query_is_error_on_status_error() -> None:
    """A non-sql client makes ``sql_query`` return status='error' →
    HandlerResponse.is_error must be True."""
    result = asyncio.run(
        handle_kb_sql_query(
            {
                "run_id": "sr",
                "phase": "sp",
                "template_name": "t",
                "kb_config": {
                    "name": "v",
                    "type": "vector",
                    "provider": "pinecone",
                    "config": {},
                },
            }
        )
    )
    assert result.is_error is True
    payload = json.loads(result.json)
    assert payload["status"] == "error"


# ── handle_kb_export_query_log ────────────────────────────────────


def test_kb_export_query_log_required_arg_guards() -> None:
    with pytest.raises(ValueError, match="run_id is required"):
        handle_kb_export_query_log({"phase": "p"})
    with pytest.raises(ValueError, match="phase is required"):
        handle_kb_export_query_log({"run_id": "r"})


def test_kb_export_query_log_shape_and_scoping() -> None:
    # Seed a couple of logged queries.
    kb_client.append_search_entry("er", "ep", "first query", 2)
    kb_client.append_search_entry("er", "ep", "second query", 0)

    result = handle_kb_export_query_log({"run_id": "er", "phase": "ep"})
    assert result.is_error is False
    payload = json.loads(result.json)
    assert list(payload.keys()) == ["run_id", "phase", "queries"]
    assert payload["run_id"] == "er"
    assert payload["phase"] == "ep"
    assert len(payload["queries"]) == 2

    # debate_transcript_id scoping appends the key (after queries).
    scoped = handle_kb_export_query_log(
        {"run_id": "er", "phase": "ep", "debate_transcript_id": "dt-9"}
    )
    scoped_payload = json.loads(scoped.json)
    assert list(scoped_payload.keys()) == [
        "run_id",
        "phase",
        "queries",
        "debate_transcript_id",
    ]
    assert scoped_payload["debate_transcript_id"] == "dt-9"


def test_kb_export_query_log_preserves_raw_non_ascii() -> None:
    """ensure_ascii=False regression: a non-ASCII query must stay raw in the
    exported JSON, never \\uXXXX-escaped (the prior-task BLOCKING bug)."""
    kb_client.append_search_entry("ur", "up", "café — résumé", 1)
    result = handle_kb_export_query_log({"run_id": "ur", "phase": "up"})
    assert "café — résumé" in result.json
    assert "\\u00e9" not in result.json
    assert "\\u2014" not in result.json


# ── handle_kb_build_index ─────────────────────────────────────────


def test_kb_build_index_required_arg_guard(tmp_path: object) -> None:
    ctx = make_ctx(str(tmp_path))
    with pytest.raises(ValueError, match="collection_name is required"):
        handle_kb_build_index({}, ctx)


def test_kb_build_index_empty_collection_raises_byte_exact(tmp_path: object) -> None:
    ctx = make_ctx(str(tmp_path))
    # Seed an EMPTY collection so query_items returns [] (rather than raising
    # "not loaded").
    _seed_collection("empty_col", [])
    with pytest.raises(ValueError) as excinfo:
        handle_kb_build_index({"collection_name": "empty_col"}, ctx)
    assert str(excinfo.value) == (
        'Collection "empty_col" not found or empty. Load it with '
        "pipeline_cc_load_collection first."
    )


def test_kb_build_index_happy_build_and_response_shape(tmp_path: object) -> None:
    base_dir = str(tmp_path)
    ctx = make_ctx(base_dir)
    items = [
        make_item("g1", "alpha beta gamma", title="G1"),
        make_item("g2", "delta epsilon", title="G2"),
    ]
    _seed_collection("built_col", items)

    result = handle_kb_build_index({"collection_name": "built_col"}, ctx)
    assert result.is_error is False
    payload = json.loads(result.json)
    # Frozen key order.
    assert list(payload.keys()) == [
        "status",
        "collection_name",
        "index_path",
        "item_count",
        "avg_doc_length",
        "next_step",
    ]
    assert payload["status"] == "ok"
    assert payload["collection_name"] == "built_col"
    assert payload["item_count"] == 2
    assert payload["next_step"] == (
        "BM25 index built. Use pipeline_kb_search with query to search."
    )
    expected_dir = bm25_index_dir(base_dir, "built_col")
    assert payload["index_path"] == expected_dir
    # The index was actually persisted to disk.
    assert os.path.exists(os.path.join(expected_dir, "index.json"))
    # And it is now searchable end-to-end.
    search = asyncio.run(
        handle_kb_search({"run_id": "r", "phase": "p", "query": "alpha"}, ctx)
    )
    search_payload = json.loads(search.json)
    assert search_payload["results_count"] == 1
    assert search_payload["results"][0]["item_id"] == "g1"


# ── register_kb_tools — registration integrity ────────────────────


def test_register_kb_tools_enumeration_and_metadata() -> None:
    """Exactly the four ``pipeline_kb_*`` tools enumerate, all unique, all with
    the ``pipeline_`` prefix, non-empty descriptions, inputSchema present, and
    the correct annotations (readOnly×3, destructive×1) + ``_meta``."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("pipeline")
    register_kb_tools(mcp)

    tools = asyncio.run(mcp.list_tools())
    by_name = {t.name: t for t in tools}

    expected_names = {
        "pipeline_kb_search",
        "pipeline_kb_sql_query",
        "pipeline_kb_build_index",
        "pipeline_kb_export_query_log",
    }
    assert set(by_name) == expected_names
    assert len(tools) == 4
    assert len(by_name) == 4, "tool names must be unique"

    for name, tool in by_name.items():
        assert name.startswith("pipeline_"), name
        assert tool.description, f"{name} must have a non-empty description"
        assert tool.inputSchema is not None, f"{name} must have an inputSchema"

    expected_max = {
        "pipeline_kb_search": 50000,
        "pipeline_kb_sql_query": 50000,
        "pipeline_kb_build_index": 10000,
        "pipeline_kb_export_query_log": 50000,
    }
    for name, expected in expected_max.items():
        assert by_name[name].meta == {"max_result_chars": expected}, name

    read_only = (
        "pipeline_kb_search",
        "pipeline_kb_sql_query",
        "pipeline_kb_export_query_log",
    )
    for name in read_only:
        ann = by_name[name].annotations
        assert ann is not None, f"{name} must carry annotations"
        assert ann.readOnlyHint is True, name
        assert ann.destructiveHint is None, name

    build = by_name["pipeline_kb_build_index"].annotations
    assert build is not None
    assert build.destructiveHint is True
    assert build.readOnlyHint is None
