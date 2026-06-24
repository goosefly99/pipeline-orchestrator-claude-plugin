"""Focused unit tests for :mod:`pipeline_orchestrator.web_search`.

There is no ``web-search.test.ts`` in the Node suite (web-search is exercised
only via integration tests), so these tests are authored here to lock the
byte-exact behavioral contract of the port: provider selection + the empty-
string-survives default, the Brave/DDG body mapping and slicing, the DDG
``FirstURL && Text`` filter + ``Text[:120]`` truncation, the no-key ValueError,
``fetch_and_extract`` per-URL failure isolation, the byte-exact no-trailing-
newline merge (with a non-ASCII title proving ``ensure_ascii=False``), the
RawItem/SourceRun key order + the undefined-key-drop for SourceRun ``query``,
and the two-mode :func:`execute_web_search` branch incl. the neither-arg raise
and ``max_results_per_query`` nullish (explicit ``0`` survives, absent → 5).

All HTTP is monkeypatched at the ``_http_get_json`` / ``_http_get_text`` seams
— NO network. Coroutines are driven with ``asyncio.run(...)`` inside sync test
bodies (the suite has no async-test plugin), matching ``test_ingest.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from pipeline_orchestrator import web_search as ws

# ── Provider selection ────────────────────────────────────────


class TestProviderSelection:
    def test_default_provider_is_duckduckgo_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PIPELINE_SEARCH_API", raising=False)
        called: dict[str, object] = {}

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            called["url"] = url
            return (
                200,
                "OK",
                {
                    "RelatedTopics": [
                        {"FirstURL": "https://a.example", "Text": "Alpha"}
                    ]
                },
            )

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        results = asyncio.run(ws.web_search(["alpha"], 5))

        assert called["url"] == "https://api.duckduckgo.com/"
        assert results == [
            {"title": "Alpha", "url": "https://a.example", "snippet": "Alpha"}
        ]

    def test_empty_string_provider_survives_to_duckduckgo(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Empty string is PRESENT (not absent) → must NOT be re-defaulted; it
        # falls through to the DuckDuckGo branch (nullish, not falsy).
        monkeypatch.setenv("PIPELINE_SEARCH_API", "")
        called: dict[str, object] = {}

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            called["url"] = url
            return 200, "OK", {"RelatedTopics": []}

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        asyncio.run(ws.web_search(["q"], 5))
        assert called["url"] == "https://api.duckduckgo.com/"

    def test_brave_provider_with_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_SEARCH_API", "brave")
        monkeypatch.setenv("BRAVE_API_KEY", "secret-token")
        seen: dict[str, object] = {}

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            seen["url"] = url
            seen["params"] = params
            seen["headers"] = headers
            return (
                200,
                "OK",
                {
                    "web": {
                        "results": [
                            {
                                "title": "T1",
                                "url": "https://1.example",
                                "description": "D1",
                            },
                            {
                                "title": "T2",
                                "url": "https://2.example",
                                "description": "D2",
                            },
                            {
                                "title": "T3",
                                "url": "https://3.example",
                                "description": "D3",
                            },
                        ]
                    }
                },
            )

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        results = asyncio.run(ws.web_search(["q"], 2))

        assert seen["url"] == "https://api.search.brave.com/res/v1/web/search"
        assert seen["params"] == {"q": "q", "count": "2"}
        assert seen["headers"] == {
            "X-Subscription-Token": "secret-token",
            "Accept": "application/json",
        }
        # sliced to max=2, key order title,url,snippet
        assert results == [
            {"title": "T1", "url": "https://1.example", "snippet": "D1"},
            {"title": "T2", "url": "https://2.example", "snippet": "D2"},
        ]
        assert list(results[0].keys()) == ["title", "url", "snippet"]

    def test_brave_provider_without_key_raises_exact(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_SEARCH_API", "brave")
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)

        with pytest.raises(ValueError) as exc:
            asyncio.run(ws.web_search(["q"], 5))
        assert (
            str(exc.value)
            == "BRAVE_API_KEY env var required when PIPELINE_SEARCH_API=brave"
        )

    def test_brave_non_2xx_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_SEARCH_API", "brave")
        monkeypatch.setenv("BRAVE_API_KEY", "k")

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            return 503, "Service Unavailable", {}

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        with pytest.raises(ValueError) as exc:
            asyncio.run(ws.web_search(["q"], 5))
        assert str(exc.value) == "Brave search failed: 503 Service Unavailable"

    def test_brave_nullish_results_default_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_SEARCH_API", "brave")
        monkeypatch.setenv("BRAVE_API_KEY", "k")

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            return 200, "OK", {}  # no "web" key → nullish → []

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        assert asyncio.run(ws.web_search(["q"], 5)) == []

    def test_duckduckgo_non_2xx_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PIPELINE_SEARCH_API", raising=False)

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            return 429, "Too Many Requests", {}

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        with pytest.raises(ValueError) as exc:
            asyncio.run(ws.web_search(["q"], 5))
        assert (
            str(exc.value) == "DuckDuckGo search failed: 429 Too Many Requests"
        )

    def test_duckduckgo_filter_slice_and_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PIPELINE_SEARCH_API", raising=False)
        long_text = "x" * 200

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            assert params == {
                "q": "q",
                "format": "json",
                "no_redirect": "1",
            }
            return (
                200,
                "OK",
                {
                    "RelatedTopics": [
                        # kept (both truthy), Text truncated to 120
                        {"FirstURL": "https://keep.example", "Text": long_text},
                        # dropped: no FirstURL
                        {"FirstURL": "", "Text": "no url"},
                        # dropped: no Text
                        {"FirstURL": "https://x.example", "Text": ""},
                        # missing keys entirely → dropped
                        {"Result": "raw"},
                        # second keeper, but max=1 slices it off
                        {"FirstURL": "https://b.example", "Text": "Bee"},
                    ]
                },
            )

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        results = asyncio.run(ws.web_search(["q"], 1))

        assert len(results) == 1
        assert results[0]["url"] == "https://keep.example"
        assert results[0]["title"] == long_text[:120]
        assert len(results[0]["title"]) == 120
        assert results[0]["snippet"] == long_text  # full text, not truncated

    def test_web_search_loops_queries_and_extends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PIPELINE_SEARCH_API", raising=False)

        async def fake_json(
            url: str, params: dict[str, str], headers: dict[str, str]
        ) -> tuple[int, str, object]:
            q = params["q"]
            return (
                200,
                "OK",
                {
                    "RelatedTopics": [
                        {"FirstURL": f"https://{q}.example", "Text": q}
                    ]
                },
            )

        monkeypatch.setattr(ws, "_http_get_json", fake_json)
        results = asyncio.run(ws.web_search(["a", "b"], 5))
        assert [r["url"] for r in results] == [
            "https://a.example",
            "https://b.example",
        ]


# ── fetch_and_extract ─────────────────────────────────────────


class TestFetchAndExtract:
    def test_success_and_failure_in_one_batch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        html = (
            "<html><head><title>  My   Page  </title></head>"
            "<body><p>Hello world</p></body></html>"
        )

        async def fake_text(
            url: str, headers: dict[str, str]
        ) -> tuple[int, str]:
            assert headers == {"User-Agent": "PipelineMCP/1.0"}
            if url == "https://ok.example":
                return 200, html
            raise RuntimeError("boom")

        monkeypatch.setattr(ws, "_http_get_text", fake_text)
        results = asyncio.run(
            ws.fetch_and_extract(
                ["https://ok.example", "https://bad.example"]
            )
        )

        # One failure does not abort the batch.
        assert len(results) == 2

        ok = results[0]
        assert ok["url"] == "https://ok.example"
        assert ok["title"] == "My Page"  # collapsed whitespace, trimmed
        assert ok["status"] == 200
        ok_content = ok["content"]
        assert isinstance(ok_content, str)
        assert "Hello world" in ok_content
        assert ok["content_length"] == len(ok_content)
        assert "error" not in ok

        bad = results[1]
        assert bad == {
            "url": "https://bad.example",
            "title": "https://bad.example",
            "content": "",
            "status": 0,
            "content_length": 0,
            "error": "boom",
        }

    def test_title_falls_back_to_url_when_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_text(
            url: str, headers: dict[str, str]
        ) -> tuple[int, str]:
            return 200, "<html><body>no title here</body></html>"

        monkeypatch.setattr(ws, "_http_get_text", fake_text)
        results = asyncio.run(ws.fetch_and_extract(["https://t.example"]))
        assert results[0]["title"] == "https://t.example"


# ── merge_into_collection ─────────────────────────────────────


class TestMergeIntoCollection:
    def test_appends_in_order_and_byte_exact_no_trailing_newline(
        self, tmp_path: Path
    ) -> None:
        existing: dict[str, object] = {
            "collection_id": "c--abc",
            "created_date": "2026-01-01T00:00:00.000Z",
            "status": "raw",
            "source_runs": [{"source_type": "web", "url": "https://old"}],
            "items": [{"id": "web_old", "title": "old"}],
        }
        path = tmp_path / "raw-collection.json"
        path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        new_items: list[dict[str, object]] = [
            {"id": "web_new", "title": "Café résumé — naïve"}  # non-ASCII
        ]
        new_runs: list[dict[str, object]] = [
            {"source_type": "web", "url": "https://new"}
        ]

        ws.merge_into_collection(str(path), new_items, new_runs)

        on_disk = path.read_text(encoding="utf-8")

        # Appended in order.
        reparsed = json.loads(on_disk)
        assert [i["id"] for i in reparsed["items"]] == ["web_old", "web_new"]
        assert [s["url"] for s in reparsed["source_runs"]] == [
            "https://old",
            "https://new",
        ]

        # Byte-exact vs json.dumps(..., indent=2, ensure_ascii=False), and NO
        # trailing newline.
        expected_obj = dict(existing)
        old_items = existing["items"]
        assert isinstance(old_items, list)
        old_runs = existing["source_runs"]
        assert isinstance(old_runs, list)
        expected_obj["items"] = old_items + new_items
        expected_obj["source_runs"] = old_runs + new_runs
        expected = json.dumps(expected_obj, indent=2, ensure_ascii=False)
        assert on_disk == expected
        assert not on_disk.endswith("\n")

        # ensure_ascii=False preserved raw UTF-8 (no \uXXXX escapes).
        assert "Café résumé — naïve" in on_disk
        assert "\\u" not in on_disk


# ── _build_web_items ──────────────────────────────────────────


class TestBuildWebItems:
    def test_key_order_and_query_present_when_given(self) -> None:
        fetched = [
            {
                "url": "https://p.example",
                "title": "Page",
                "content": "body",
                "status": 200,
                "content_length": 4,
            }
        ]
        items, runs = ws._build_web_items(
            fetched, "my query", {"https://p.example": "snip"}
        )

        assert len(items) == 1
        item = items[0]
        assert list(item.keys()) == [
            "id",
            "source_type",
            "source_ref",
            "title",
            "content",
            "url",
            "date",
            "tags",
            "metadata",
        ]
        item_id = item["id"]
        assert isinstance(item_id, str)
        assert item_id.startswith("web_")
        assert item["source_type"] == "web"
        assert item["source_ref"] == "url:https://p.example"
        assert item["title"] == "Page"
        assert item["content"] == "body"
        assert item["url"] == "https://p.example"
        assert item["tags"] == []
        assert isinstance(item["date"], str) and item["date"].endswith("Z")

        meta = item["metadata"]
        assert isinstance(meta, dict)
        assert list(meta.keys()) == [
            "search_query",
            "search_snippet",
            "fetch_status",
            "content_length",
        ]
        assert meta["search_query"] == "my query"
        assert meta["search_snippet"] == "snip"
        assert meta["fetch_status"] == 200
        assert meta["content_length"] == 4

        assert len(runs) == 1
        run = runs[0]
        # query PRESENT (and in correct position) when search_query given.
        assert list(run.keys()) == [
            "source_type",
            "query",
            "url",
            "executed_at",
            "items_found",
            "items_stored",
        ]
        assert run["query"] == "my query"
        assert run["url"] == "https://p.example"
        assert run["items_found"] == 1
        assert run["items_stored"] == 1
        # item.date and run.executed_at share the single _now_iso() call.
        assert run["executed_at"] == item["date"]

    def test_query_key_omitted_when_none(self) -> None:
        fetched = [
            {
                "url": "https://p.example",
                "title": "Page",
                "content": "body",
                "status": 200,
                "content_length": 4,
            }
        ]
        items, runs = ws._build_web_items(fetched, None, {})

        # Undefined-key-drop: no "query" key at all (NOT "query": None).
        assert "query" not in runs[0]
        assert list(runs[0].keys()) == [
            "source_type",
            "url",
            "executed_at",
            "items_found",
            "items_stored",
        ]
        # search_query nullish → "" (not None).
        meta = items[0]["metadata"]
        assert isinstance(meta, dict)
        assert meta["search_query"] == ""
        # snippet absent → "" via .get default.
        assert meta["search_snippet"] == ""

    def test_skips_entries_with_error(self) -> None:
        fetched = [
            {"url": "https://bad", "error": "nope", "status": 0,
             "content": "", "title": "https://bad", "content_length": 0},
            {"url": "https://good", "title": "G", "content": "c",
             "status": 200, "content_length": 1},
        ]
        items, runs = ws._build_web_items(fetched, "q", {})
        assert len(items) == 1
        assert len(runs) == 1
        assert items[0]["url"] == "https://good"


# ── execute_web_search (two-mode branch) ──────────────────────


def _write_collection(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "collection_id": "c--seed",
                "created_date": "2026-01-01T00:00:00.000Z",
                "status": "raw",
                "source_runs": [],
                "items": [],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class TestExecuteWebSearch:
    def test_search_only_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        canned = [
            {"title": "T", "url": "https://r.example", "snippet": "S"}
        ]

        async def fake_web_search(
            queries: list[str], max_results: int
        ) -> list[dict[str, str]]:
            assert queries == ["hello"]
            assert max_results == 5  # absent → 5
            return canned

        monkeypatch.setattr(ws, "web_search", fake_web_search)
        col = tmp_path / "c.json"
        _write_collection(col)

        out = asyncio.run(
            ws.execute_web_search(
                {"queries": ["hello"], "collection_path": str(col)}
            )
        )
        assert out["data"] == {
            "search_results": canned,
            "result_count": 1,
        }
        assert out["next_step"] == (
            "Review results and call again with fetch_urls to ingest "
            "selected pages."
        )

    def test_fetch_only_mode(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        async def fake_fetch(urls: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "url": "https://ok",
                    "title": "OK",
                    "content": "body",
                    "status": 200,
                    "content_length": 4,
                },
                {
                    "url": "https://bad",
                    "title": "https://bad",
                    "content": "",
                    "status": 0,
                    "content_length": 0,
                    "error": "boom",
                },
            ]

        monkeypatch.setattr(ws, "fetch_and_extract", fake_fetch)
        col = tmp_path / "c.json"
        _write_collection(col)

        out = asyncio.run(
            ws.execute_web_search(
                {
                    "fetch_urls": ["https://ok", "https://bad"],
                    "collection_path": str(col),
                }
            )
        )
        data = out["data"]
        assert isinstance(data, dict)
        # Fetch-mode key order, NO search_results (search not run).
        assert list(data.keys()) == [
            "items_added",
            "items_failed",
            "errors",
            "collection_path",
            "fetched_urls",
        ]
        assert data["items_added"] == 1
        assert data["items_failed"] == 1
        assert data["errors"] == ["https://bad: boom"]
        assert data["collection_path"] == str(col)
        assert data["fetched_urls"] == ["https://ok", "https://bad"]
        assert "search_results" not in data
        assert out["next_step"] == (
            "Call pipeline_register_artifact to register the updated "
            "collection."
        )

        # The merge actually landed the one good item on disk.
        reparsed = json.loads(col.read_text(encoding="utf-8"))
        assert len(reparsed["items"]) == 1
        assert reparsed["items"][0]["url"] == "https://ok"

    def test_both_modes_appends_search_results_last(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        canned = [
            {"title": "T", "url": "https://ok", "snippet": "Snippet!"}
        ]

        async def fake_web_search(
            queries: list[str], max_results: int
        ) -> list[dict[str, str]]:
            assert max_results == 0  # explicit 0 survives (nullish)
            return canned

        async def fake_fetch(urls: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "url": "https://ok",
                    "title": "OK",
                    "content": "body",
                    "status": 200,
                    "content_length": 4,
                }
            ]

        monkeypatch.setattr(ws, "web_search", fake_web_search)
        monkeypatch.setattr(ws, "fetch_and_extract", fake_fetch)
        col = tmp_path / "c.json"
        _write_collection(col)

        out = asyncio.run(
            ws.execute_web_search(
                {
                    "queries": ["q"],
                    "fetch_urls": ["https://ok"],
                    "max_results_per_query": 0,
                    "collection_path": str(col),
                }
            )
        )
        data = out["data"]
        assert isinstance(data, dict)
        # search_results + result_count appended LAST.
        assert list(data.keys()) == [
            "items_added",
            "items_failed",
            "errors",
            "collection_path",
            "fetched_urls",
            "search_results",
            "result_count",
        ]
        assert data["search_results"] == canned
        assert data["result_count"] == 1

        # The snippet from search flowed into the item's metadata.
        reparsed = json.loads(col.read_text(encoding="utf-8"))
        assert reparsed["items"][0]["metadata"]["search_snippet"] == "Snippet!"
        assert reparsed["items"][0]["metadata"]["search_query"] == "q"

    def test_empty_search_results_still_counts_as_provided(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # An EMPTY search_results list is not None → still appended to the
        # fetch-mode data (mirrors JS truthiness on the variable, not falsy).
        async def fake_web_search(
            queries: list[str], max_results: int
        ) -> list[dict[str, str]]:
            return []

        async def fake_fetch(urls: list[str]) -> list[dict[str, object]]:
            return [
                {
                    "url": "https://ok",
                    "title": "OK",
                    "content": "b",
                    "status": 200,
                    "content_length": 1,
                }
            ]

        monkeypatch.setattr(ws, "web_search", fake_web_search)
        monkeypatch.setattr(ws, "fetch_and_extract", fake_fetch)
        col = tmp_path / "c.json"
        _write_collection(col)

        out = asyncio.run(
            ws.execute_web_search(
                {
                    "queries": ["q"],
                    "fetch_urls": ["https://ok"],
                    "collection_path": str(col),
                }
            )
        )
        data = out["data"]
        assert isinstance(data, dict)
        assert data["search_results"] == []
        assert data["result_count"] == 0

    def test_neither_arg_raises_exact(self, tmp_path: Path) -> None:
        col = tmp_path / "c.json"
        _write_collection(col)
        with pytest.raises(ValueError) as exc:
            asyncio.run(
                ws.execute_web_search({"collection_path": str(col)})
            )
        assert (
            str(exc.value)
            == "Either queries or fetch_urls (or both) must be provided."
        )

    def test_empty_lists_are_neither_mode(self, tmp_path: Path) -> None:
        # Empty queries/fetch_urls lists fail the ``len > 0`` guard → neither.
        col = tmp_path / "c.json"
        _write_collection(col)
        with pytest.raises(ValueError) as exc:
            asyncio.run(
                ws.execute_web_search(
                    {
                        "queries": [],
                        "fetch_urls": [],
                        "collection_path": str(col),
                    }
                )
            )
        assert (
            str(exc.value)
            == "Either queries or fetch_urls (or both) must be provided."
        )
