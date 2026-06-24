"""Port of ``legacy-node/tests/collections.test.ts`` (39 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.collections_store``.
Paths are built OS-agnostically with ``os.path.join`` and rooted at the pytest
``tmp_path`` fixture; JSON round-trips via ``json.loads``. The shared
module-level ``STORE`` persists across the suite, so each ``loadCollection``
uses a distinct collection name (mirroring the Node tests, which do the same).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from pipeline_orchestrator.collections_store import (
    detect_field_map,
    get_collection,
    get_items,
    list_collections,
    load_collection,
    persist_curated_collection,
    persist_raw_collection,
    query_items,
    resolve_collections_dir,
)


def write_json(tmp_path: Path, name: str, data: object) -> str:
    """Write ``data`` as compact JSON to ``tmp_path/name`` (mirrors ``writeJson``)."""
    p = os.path.join(str(tmp_path), name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, separators=(",", ":")))
    return p


# ── detectFieldMap ────────────────────────────────────────────────


def test_detects_standard_field_names() -> None:
    data: dict[str, object] = {
        "items": [
            {"id": "1", "content": "hello", "title": "hi", "tags": ["a"]},
        ],
    }
    fm = detect_field_map(data)
    assert fm.items_key == "items"
    assert fm.id_field == "id"
    assert fm.content_field == "content"
    assert fm.title_field == "title"
    assert fm.tags_field == "tags"


def test_detects_non_standard_field_names() -> None:
    data: dict[str, object] = {
        "posts": [
            {
                "tweet_id": "t1",
                "body": "hello world",
                "headline": "News",
                "categories": ["finance"],
                "profile": "alice",
            },
        ],
    }
    fm = detect_field_map(data)
    assert fm.items_key == "posts"
    assert fm.id_field == "tweet_id"
    assert fm.content_field == "body"
    assert fm.title_field == "headline"
    assert fm.tags_field == "categories"
    assert fm.author_field == "profile"


def test_detects_id_suffix_fallback() -> None:
    data: dict[str, object] = {
        "items": [
            {"article_id": "a1", "text": "content"},
        ],
    }
    fm = detect_field_map(data)
    assert fm.id_field == "article_id"


def test_detects_date_and_url_fields() -> None:
    data: dict[str, object] = {
        "items": [
            {
                "id": "1",
                "content": "x",
                "created_at": "2026-01-01",
                "permalink": "https://example.com",
            },
        ],
    }
    fm = detect_field_map(data)
    assert fm.date_field == "created_at"
    assert fm.url_field == "permalink"


def test_applies_overrides_over_autodetected() -> None:
    data: dict[str, object] = {
        "items": [
            {"id": "1", "content": "x", "title": "y", "tags": []},
        ],
    }
    fm = detect_field_map(data, {"content_field": "title", "tags_field": "custom_tags"})
    assert fm.content_field == "title"
    assert fm.tags_field == "custom_tags"
    # Non-overridden fields still auto-detected.
    assert fm.id_field == "id"


def test_throws_when_no_items_array_found() -> None:
    with pytest.raises(ValueError, match="No items array found"):
        detect_field_map({"metadata": "nothing here"})


def test_throws_when_items_array_is_empty() -> None:
    with pytest.raises(ValueError, match="No items array found"):
        detect_field_map({"items": []})


def test_defaults_to_id_when_no_id_field_or_suffix() -> None:
    data: dict[str, object] = {
        "items": [
            {"name": "test", "content": "x"},
        ],
    }
    fm = detect_field_map(data)
    assert fm.id_field == "id"


# ── loadCollection ────────────────────────────────────────────────


def test_loads_valid_collection_from_json(tmp_path: Path) -> None:
    data = {
        "items": [
            {
                "id": "item1",
                "content": "Research on X",
                "title": "X Research",
                "tags": ["ml"],
            },
            {
                "id": "item2",
                "content": "Research on Y",
                "title": "Y Research",
                "tags": ["ml", "nlp"],
            },
        ],
    }
    path = write_json(tmp_path, "research.json", data)
    col = load_collection(path, "test-research")

    assert col.name == "test-research"
    assert len(col.items) == 2
    assert col.items[0].id == "item1"
    assert col.items[0].content == "Research on X"
    assert col.available_tags == ["ml", "nlp"]


def test_defaults_name_to_filename_without_extension(tmp_path: Path) -> None:
    data = {"items": [{"id": "1", "content": "x"}]}
    path = write_json(tmp_path, "my-data.json", data)
    col = load_collection(path)

    assert col.name == "my-data"


def test_normalizes_author_from_string(tmp_path: Path) -> None:
    data = {
        "items": [
            {"id": "1", "content": "x", "author": "Alice"},
        ],
    }
    path = write_json(tmp_path, "authors-str.json", data)
    col = load_collection(path, "authors-str")
    assert col.items[0].author is not None
    assert col.items[0].author.name == "Alice"


def test_normalizes_author_from_object(tmp_path: Path) -> None:
    data = {
        "items": [
            {
                "id": "1",
                "content": "x",
                "author": {"display_name": "Bob", "handle": "@bob", "bio": "dev"},
            },
        ],
    }
    path = write_json(tmp_path, "authors-obj.json", data)
    col = load_collection(path, "authors-obj")
    assert col.items[0].author is not None
    assert col.items[0].author.name == "Bob"
    assert col.items[0].author.handle == "@bob"
    assert col.items[0].author.bio == "dev"


def test_collects_remaining_fields_as_metadata(tmp_path: Path) -> None:
    data = {
        "items": [
            {
                "id": "1",
                "content": "x",
                "title": "t",
                "tags": [],
                "custom_score": 0.95,
                "source": "api",
            },
        ],
    }
    path = write_json(tmp_path, "metadata-test.json", data)
    col = load_collection(path, "metadata-test")
    assert col.items[0].metadata["custom_score"] == 0.95
    assert col.items[0].metadata["source"] == "api"


def test_assigns_fallback_id_when_id_field_missing(tmp_path: Path) -> None:
    data = {
        "items": [
            {"content": "no id here"},
            {"content": "also no id"},
        ],
    }
    path = write_json(tmp_path, "no-id.json", data)
    col = load_collection(path, "no-id")
    assert col.items[0].id == "item_0"
    assert col.items[1].id == "item_1"


def test_throws_on_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_collection(os.path.join(str(tmp_path), "nonexistent.json"))


def test_throws_on_invalid_json(tmp_path: Path) -> None:
    path = os.path.join(str(tmp_path), "bad.json")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{ invalid json")
    with pytest.raises(json.JSONDecodeError):
        load_collection(path)


def test_respects_field_overrides_for_items_key(tmp_path: Path) -> None:
    data = {
        "articles": [
            {"id": "a1", "body": "article content"},
        ],
    }
    path = write_json(tmp_path, "articles.json", data)
    col = load_collection(
        path, "articles-override", {"items_key": "articles", "content_field": "body"}
    )
    assert len(col.items) == 1
    assert col.items[0].content == "article content"


# ── queryItems ────────────────────────────────────────────────────


def test_filters_by_tags(tmp_path: Path) -> None:
    data = {
        "items": [
            {"id": "1", "content": "a", "tags": ["ml"]},
            {"id": "2", "content": "b", "tags": ["finance"]},
            {"id": "3", "content": "c", "tags": ["ml", "finance"]},
        ],
    }
    path = write_json(tmp_path, "query-tags.json", data)
    load_collection(path, "query-tags")

    results = query_items(collection="query-tags", tags=["finance"])
    assert len(results) >= 2
    assert all("finance" in r.tags for r in results)


def test_full_text_search_matches_content_title_tags(tmp_path: Path) -> None:
    data = {
        "items": [
            {
                "id": "1",
                "content": "machine learning overview",
                "title": "ML",
                "tags": [],
            },
            {"id": "2", "content": "financial report", "title": "Q4", "tags": []},
        ],
    }
    path = write_json(tmp_path, "query-search.json", data)
    load_collection(path, "query-search")

    results = query_items(collection="query-search", search="machine")
    assert len(results) == 1
    assert results[0].id == "1"


def test_respects_limit_parameter(tmp_path: Path) -> None:
    items = [
        {"id": f"i{i}", "content": f"item {i}", "tags": ["all"]} for i in range(50)
    ]
    data = {"items": items}
    path = write_json(tmp_path, "query-limit.json", data)
    load_collection(path, "query-limit")

    results = query_items(collection="query-limit", tags=["all"], limit=5)
    assert len(results) == 5


def test_query_throws_on_unknown_collection() -> None:
    with pytest.raises(ValueError, match="not loaded"):
        query_items(collection="nonexistent-collection-xyz")


# ── getItems ──────────────────────────────────────────────────────


def test_retrieves_items_by_id(tmp_path: Path) -> None:
    data = {
        "items": [
            {"id": "x1", "content": "first"},
            {"id": "x2", "content": "second"},
            {"id": "x3", "content": "third"},
        ],
    }
    path = write_json(tmp_path, "getitems.json", data)
    load_collection(path, "getitems")

    results = get_items("getitems", ["x1", "x3"])
    assert len(results) == 2
    assert any(r.id == "x1" for r in results)
    assert any(r.id == "x3" for r in results)


def test_getitems_throws_on_unknown_collection() -> None:
    with pytest.raises(ValueError, match="not loaded"):
        get_items("nonexistent-xyz", ["1"])


# ── getCollection and listCollections ─────────────────────────────


def test_returns_previously_loaded_collection(tmp_path: Path) -> None:
    data = {"items": [{"id": "1", "content": "x"}]}
    path = write_json(tmp_path, "getcol.json", data)
    load_collection(path, "getcol-test")

    col = get_collection("getcol-test")
    assert col is not None
    assert col.name == "getcol-test"


def test_returns_undefined_for_unknown_name() -> None:
    assert get_collection("nonexistent-getcol") is None


def test_lists_all_loaded_collections_with_metadata(tmp_path: Path) -> None:
    data = {"items": [{"id": "1", "content": "x", "tags": ["a"]}]}
    path = write_json(tmp_path, "listcol.json", data)
    load_collection(path, "listcol-test")

    listing = list_collections()
    entry = next((c for c in listing if c["name"] == "listcol-test"), None)
    assert entry is not None
    assert entry["item_count"] == 1
    assert isinstance(entry["available_tags"], list)
    # field_map is the FieldMap dataclass instance (TS: typeof === 'object').
    assert not isinstance(entry["field_map"], (str, int, float, bool))
    assert entry["field_map"] is not None


# ── Per-run collection write helpers (Feature C) ─────────────


def test_routes_to_run_data_dir_raw(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "my-run-2026-04-10")
    dir_path = resolve_collections_dir(
        run_data_dir, os.path.join(str(tmp_path), "legacy"), "raw"
    )
    assert dir_path == os.path.join(run_data_dir, "collections", "raw")


def test_routes_to_run_data_dir_curated(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "my-run-2026-04-10")
    dir_path = resolve_collections_dir(
        run_data_dir, os.path.join(str(tmp_path), "legacy"), "curated"
    )
    assert dir_path == os.path.join(run_data_dir, "collections", "curated")


def test_falls_back_to_legacy_raw_when_undefined(tmp_path: Path) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    dir_path = resolve_collections_dir(None, legacy_base_dir, "raw")
    assert dir_path == os.path.join(legacy_base_dir, "collections", "raw")


def test_falls_back_to_legacy_curated_when_undefined(tmp_path: Path) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    dir_path = resolve_collections_dir(None, legacy_base_dir, "curated")
    assert dir_path == os.path.join(legacy_base_dir, "collections", "curated")


def test_treats_empty_string_run_data_dir_as_absent(tmp_path: Path) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy")
    dir_path = resolve_collections_dir("", legacy_base_dir, "raw")
    assert dir_path == os.path.join(legacy_base_dir, "collections", "raw")


def test_persist_raw_writes_under_run_data_dir(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "persist-raw-a")
    collection = {"collection_id": "raw-abc", "status": "raw", "items": []}

    written_path = persist_raw_collection(
        run_data_dir,
        os.path.join(str(tmp_path), "legacy"),
        collection,
        "raw-abc.json",
    )

    expected = os.path.join(run_data_dir, "collections", "raw", "raw-abc.json")
    assert written_path == expected
    assert os.path.exists(expected), f"expected file at {expected}"
    parsed = json.loads(Path(expected).read_text(encoding="utf-8"))
    assert parsed["collection_id"] == "raw-abc"
    assert parsed["status"] == "raw"


def test_persist_raw_falls_back_to_legacy_when_absent(tmp_path: Path) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-base-raw")
    collection = {"collection_id": "legacy-raw", "items": []}

    written_path = persist_raw_collection(
        None,
        legacy_base_dir,
        collection,
        "legacy-raw.json",
    )

    expected = os.path.join(legacy_base_dir, "collections", "raw", "legacy-raw.json")
    assert written_path == expected
    assert os.path.exists(expected), f"expected file at {expected}"


def test_persist_raw_creates_target_directory_tree(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "deep", "nested", "target")
    collection = {"collection_id": "deep", "items": []}

    persist_raw_collection(run_data_dir, str(tmp_path), collection, "deep.json")

    dir_path = os.path.join(run_data_dir, "collections", "raw")
    assert os.path.exists(dir_path), f"expected mkdir to create {dir_path}"


def test_persist_raw_overwrites_existing_file(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "overwrite-ok")
    first = {"collection_id": "x", "version": 1}
    second = {"collection_id": "x", "version": 2}

    path1 = persist_raw_collection(run_data_dir, str(tmp_path), first, "x.json")
    path2 = persist_raw_collection(run_data_dir, str(tmp_path), second, "x.json")

    assert path1 == path2
    parsed = json.loads(Path(path2).read_text(encoding="utf-8"))
    assert parsed["version"] == 2, "second write should overwrite the first"


def test_persist_curated_writes_under_run_data_dir(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "persist-curated-a")
    collection = {"collection_id": "curated-1", "status": "curated", "items": []}

    written_path = persist_curated_collection(
        run_data_dir,
        os.path.join(str(tmp_path), "legacy"),
        collection,
        "curated-1.json",
    )

    expected = os.path.join(run_data_dir, "collections", "curated", "curated-1.json")
    assert written_path == expected
    assert os.path.exists(expected)
    parsed = json.loads(Path(expected).read_text(encoding="utf-8"))
    assert parsed["collection_id"] == "curated-1"
    assert parsed["status"] == "curated"


def test_persist_curated_falls_back_to_legacy_when_absent(tmp_path: Path) -> None:
    legacy_base_dir = os.path.join(str(tmp_path), "legacy-base-curated")
    collection = {"collection_id": "legacy-curated", "items": []}

    written_path = persist_curated_collection(
        None,
        legacy_base_dir,
        collection,
        "legacy-curated.json",
    )

    expected = os.path.join(
        legacy_base_dir, "collections", "curated", "legacy-curated.json"
    )
    assert written_path == expected
    assert os.path.exists(expected)


def test_raw_and_curated_land_in_sibling_directories(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "both-subtypes")
    raw = {"collection_id": "r", "status": "raw"}
    curated = {"collection_id": "c", "status": "curated"}

    raw_path = persist_raw_collection(run_data_dir, str(tmp_path), raw, "r.json")
    curated_path = persist_curated_collection(
        run_data_dir, str(tmp_path), curated, "c.json"
    )

    assert raw_path == os.path.join(run_data_dir, "collections", "raw", "r.json")
    assert curated_path == os.path.join(
        run_data_dir, "collections", "curated", "c.json"
    )
    assert os.path.exists(raw_path)
    assert os.path.exists(curated_path)


def test_persist_curated_overwrites_existing_file(tmp_path: Path) -> None:
    run_data_dir = os.path.join(str(tmp_path), "runs", "curated-overwrite")
    first = {"collection_id": "x", "version": 1}
    second = {"collection_id": "x", "version": 2}

    path1 = persist_curated_collection(run_data_dir, str(tmp_path), first, "x.json")
    path2 = persist_curated_collection(run_data_dir, str(tmp_path), second, "x.json")

    assert path1 == path2
    parsed = json.loads(Path(path2).read_text(encoding="utf-8"))
    assert parsed["version"] == 2, "second write should overwrite the first"


# ── Blind-reviewer integrity fixes (additive, beyond the 39 parity cases) ──


def test_persist_writes_non_ascii_unescaped(tmp_path: Path) -> None:
    """Non-ASCII must persist as raw UTF-8, mirroring ``JSON.stringify(x,null,2)``.

    Node emits ``café`` / ``☕`` literally; Python's default ``ensure_ascii=True``
    would escape them to ``\\u00e9`` / ``\\u2615`` and break byte-parity.
    """
    run_data_dir = os.path.join(str(tmp_path), "runs", "non-ascii")
    collection = {"name": "café", "emoji": "☕", "items": []}

    written_path = persist_raw_collection(
        run_data_dir,
        os.path.join(str(tmp_path), "legacy"),
        collection,
        "cafe.json",
    )

    text = Path(written_path).read_text(encoding="utf-8")
    # Literal UTF-8 characters are present...
    assert '"café"' in text
    assert "☕" in text
    # ...and NOT \u-escaped the way ensure_ascii=True would render them.
    assert "caf\\u00e9" not in text
    assert "\\u2615" not in text


def test_normalizes_empty_dict_author_to_unknown(tmp_path: Path) -> None:
    """An empty ``{}`` author is a truthy JS object → ``{name:'Unknown'}``.

    JS ``typeof {} === 'object'`` enters the object branch; every key is
    ``undefined`` so the name coalesces to ``'Unknown'`` and handle/bio are
    ``undefined``/``None``.
    """
    data = {
        "items": [
            {"id": "1", "content": "x", "author": {}},
        ],
    }
    path = write_json(tmp_path, "authors-empty-dict.json", data)
    col = load_collection(path, "authors-empty-dict")
    assert col.items[0].author is not None
    assert col.items[0].author.name == "Unknown"
    assert col.items[0].author.handle is None
    assert col.items[0].author.bio is None


def test_normalizes_array_author_to_unknown(tmp_path: Path) -> None:
    """An array author is also a JS object (``typeof [] === 'object'``).

    Array index access by key (``a.display_name`` etc.) is ``undefined``, so the
    name falls through to ``'Unknown'`` and handle/bio are ``None`` — matching
    the TS oracle.
    """
    data = {
        "items": [
            {"id": "1", "content": "x", "author": ["a", "b"]},
        ],
    }
    path = write_json(tmp_path, "authors-array.json", data)
    col = load_collection(path, "authors-array")
    assert col.items[0].author is not None
    assert col.items[0].author.name == "Unknown"
    assert col.items[0].author.handle is None
    assert col.items[0].author.bio is None
