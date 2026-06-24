"""Port of ``legacy-node/tests/bm25.test.ts`` (18 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.bm25``. Index
directories are rooted at the pytest ``tmp_path`` fixture (mirroring the Node
tests' ``mkdtempSync`` temp dirs); paths are built OS-agnostically with
``os.path.join``.
"""

from __future__ import annotations

import os

from pipeline_orchestrator.bm25 import BM25Index, bm25_index_dir
from pipeline_orchestrator.collections_store import ResearchItem


def make_item(
    item_id: str, title: str, content: str, tags: list[str] | None = None
) -> ResearchItem:
    return ResearchItem(
        id=item_id, title=title, content=content, tags=tags or [], metadata={}
    )


SAMPLE_ITEMS: list[ResearchItem] = [
    make_item(
        "item-1",
        "Bitcoin Trading Strategies",
        "Analysis of momentum and mean-reversion strategies for BTC spot markets.",
        ["crypto", "trading"],
    ),
    make_item(
        "item-2",
        "Ethereum DeFi Yield Farming",
        "Yield farming across Uniswap, Aave, and Compound protocols.",
        ["crypto", "defi"],
    ),
    make_item(
        "item-3",
        "Prediction Market Design",
        "How prediction markets aggregate information through binary contracts.",
        ["markets", "design"],
    ),
    make_item(
        "item-4",
        "Election Forecasting Models",
        "Bayesian models for election outcome prediction using polling data.",
        ["elections", "models"],
    ),
    make_item(
        "item-5",
        "Options Pricing Theory",
        "Black-Scholes and binomial tree models for option valuation.",
        ["finance", "options"],
    ),
]


# ── build ────────────────────────────────────────────────────


def test_build_returns_correct_metadata() -> None:
    idx = BM25Index()
    meta = idx.build(SAMPLE_ITEMS, "test-collection")

    assert meta.collection_name == "test-collection"
    assert meta.item_count == 5
    assert meta.avg_doc_length > 0
    assert meta.created_at
    assert meta.item_ids == ["item-1", "item-2", "item-3", "item-4", "item-5"]


def test_build_handles_empty_items() -> None:
    idx = BM25Index()
    meta = idx.build([], "empty-col")
    assert meta.item_count == 0
    assert meta.avg_doc_length == 0
    assert meta.item_ids == []


# ── search — exact term match ────────────────────────────────


def test_search_ranks_matching_terms_highest() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "search-test")

    hits = idx.search("bitcoin trading strategies")
    assert len(hits) > 0, "should return at least one hit"
    assert hits[0].item_id == "item-1"
    assert hits[0].score > 0


# ── search — partial overlap ─────────────────────────────────


def test_search_ranks_more_matches_higher() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "overlap-test")

    hits = idx.search("election prediction models bayesian")
    assert len(hits) > 0
    assert hits[0].item_id == "item-4"


# ── search — topK ────────────────────────────────────────────


def test_search_limits_to_top_k() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "topk-test")

    hits = idx.search("market", 2)
    assert len(hits) <= 2, "should return at most 2 results"


# ── search — empty query ─────────────────────────────────────


def test_search_returns_empty_for_empty_query() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "empty-query")

    hits = idx.search("")
    assert hits == []


def test_search_returns_empty_for_short_tokens_only() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "short-tokens")

    hits = idx.search("a I")
    assert hits == []


# ── search — tag filter ──────────────────────────────────────


def test_search_only_returns_docs_matching_tag_filter() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "filter-test")

    hits = idx.search("market", 10, ["crypto"])
    for hit in hits:
        tags = [t.strip() for t in hit.metadata.tags.lower().split(",")]
        assert "crypto" in tags, f"Expected crypto tag in {hit.metadata.tags}"


def test_search_returns_empty_when_no_docs_match_tag_filter() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "no-match-filter")

    hits = idx.search("bitcoin", 10, ["nonexistent"])
    assert hits == []


# ── persist + loadIndex round-trip ───────────────────────────


def test_persist_and_load_index_round_trip(tmp_path: object) -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "persist-test")
    index_dir = os.path.join(str(tmp_path), "test-index")
    idx.persist(index_dir)

    assert os.path.exists(os.path.join(index_dir, "index.json"))

    idx2 = BM25Index()
    idx2.load_index(index_dir)

    meta = idx2.get_metadata()
    assert meta
    assert meta.collection_name == "persist-test"
    assert meta.item_count == 5

    # Search on loaded index should work identically
    hits = idx2.search("bitcoin trading")
    assert len(hits) > 0
    assert hits[0].item_id == "item-1"


# ── isBuilt ──────────────────────────────────────────────────


def test_is_built_returns_false_before_build() -> None:
    idx = BM25Index()
    assert idx.is_built() is False


def test_is_built_returns_true_after_build() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "built-test")
    assert idx.is_built() is True


def test_is_built_returns_true_when_index_json_on_disk(tmp_path: object) -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "disk-test")
    index_dir = os.path.join(str(tmp_path), "disk-index")
    idx.persist(index_dir)

    idx2 = BM25Index()
    assert idx2.is_built(index_dir) is True


def test_is_built_returns_false_for_nonexistent_directory(tmp_path: object) -> None:
    idx = BM25Index()
    assert idx.is_built(os.path.join(str(tmp_path), "no-such-dir")) is False


# ── getMetadata ──────────────────────────────────────────────


def test_get_metadata_returns_none_before_build() -> None:
    idx = BM25Index()
    assert idx.get_metadata() is None


def test_get_metadata_returns_metadata_after_build() -> None:
    idx = BM25Index()
    idx.build(SAMPLE_ITEMS, "meta-test")
    meta = idx.get_metadata()
    assert meta
    assert meta.item_count == 5


# ── bm25IndexDir ─────────────────────────────────────────────


def test_bm25_index_dir_returns_path_under_kb_bm25_collection() -> None:
    result = bm25_index_dir("/data/pipeline", "my-collection")
    assert "kb" in result
    assert "bm25" in result
    assert "my-collection" in result


def test_bm25_index_dir_handles_special_characters() -> None:
    result = bm25_index_dir("/data", "col-with-dashes_and_underscores")
    assert "col-with-dashes_and_underscores" in result
