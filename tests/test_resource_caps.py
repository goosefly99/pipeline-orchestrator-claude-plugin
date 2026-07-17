"""Focused tests for the opt-in resource caps and caches (additive — 0 NEW
parity cases; the 834-parity count is unchanged).

Five guards bound previously-unbounded growth, each preserving the byte-exact
legacy behavior when its knob is unset / on the untouched default path:

* ``PIPELINE_MCP_STORE_MAX_ITEMS`` — FIFO whole-collection eviction of the
  shared in-RAM ``collections_store.STORE``.
* the ``SecondBrainAdapter`` status-handshake cache — one ``status`` spawn per
  adapter lifetime; failed probes are never cached — plus the non-blocking
  ``asyncio`` ``default_run_cli`` runner.
* the ``kb_tools`` BM25 loaded-index cache — keyed by index dir, invalidated
  by the ``index.json`` ``(st_mtime_ns, st_size)`` signature.
* ``PIPELINE_MCP_FETCH_MAX_BYTES`` — web-fetch stored-content truncation with
  the additive ``truncated`` metadata marker (absent below the cap).
* ``PIPELINE_MCP_QUERY_LOG_MAX_ENTRIES`` — per-run FIFO cap on the in-RAM KB
  query log (evicts from both the run store and the phase index).

House idioms mirrored from the siblings (``test_kb_tools.py`` /
``test_web_search.py`` / ``test_second_brain_adapter.py``): coroutines driven
with ``asyncio.run(...)`` in sync bodies (no pytest-asyncio), env toggles via
``monkeypatch``, disk fixtures under ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import pipeline_orchestrator.kb_client as kb_client
from pipeline_orchestrator import web_search as ws
from pipeline_orchestrator.bm25 import BM25Index, bm25_index_dir
from pipeline_orchestrator.collections_store import (
    STORE,
    ResearchItem,
    load_collection,
)
from pipeline_orchestrator.kb_client import (
    KBClient,
    VectorSearchParams,
    append_search_entry,
    clear_query_log,
    export_query_log,
    get_query_log,
)
from pipeline_orchestrator.second_brain_adapter import (
    RunCliResult,
    create_second_brain_adapter,
    default_run_cli,
)
from pipeline_orchestrator.tools.kb_tools import KBContext, handle_kb_search

# ── Shared-state isolation ────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate_shared_state(monkeypatch: pytest.MonkeyPatch) -> object:
    """Snapshot/restore the shared STORE, reset the kb_client stores, and keep
    the cap knobs + vector env gate unset unless a test opts in."""
    monkeypatch.delenv("PIPELINE_MCP_STORE_MAX_ITEMS", raising=False)
    monkeypatch.delenv("PIPELINE_MCP_FETCH_MAX_BYTES", raising=False)
    monkeypatch.delenv("PIPELINE_MCP_QUERY_LOG_MAX_ENTRIES", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_INDEX_PATH", raising=False)
    store_snapshot = dict(STORE)
    clear_query_log()
    yield
    STORE.clear()
    STORE.update(store_snapshot)
    clear_query_log()


# ── PIPELINE_MCP_STORE_MAX_ITEMS ──────────────────────────────


def _write_collection_file(tmp_path: Path, name: str, item_count: int) -> str:
    """Write a minimal loadable collection JSON with ``item_count`` items."""
    data = {
        "items": [
            {"id": f"{name}-{i}", "content": f"content {i}", "title": f"T{i}",
             "tags": []}
            for i in range(item_count)
        ]
    }
    p = tmp_path / f"{name}.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return str(p)


class TestStoreMaxItems:
    def test_unset_is_unlimited_and_reload_keeps_position(
        self, tmp_path: Path
    ) -> None:
        """Parity pin: with the knob unset, nothing is evicted and reloading a
        collection keeps its ORIGINAL insertion position in the STORE."""
        STORE.clear()
        pa = _write_collection_file(tmp_path, "cap_a", 3)
        pb = _write_collection_file(tmp_path, "cap_b", 3)
        load_collection(pa)
        load_collection(pb)
        load_collection(pa)  # reload — must NOT move to the back
        assert list(STORE.keys()) == ["cap_a", "cap_b"]

    def test_evicts_oldest_whole_collection_over_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_STORE_MAX_ITEMS", "3")
        STORE.clear()
        load_collection(_write_collection_file(tmp_path, "cap_a", 2))
        load_collection(_write_collection_file(tmp_path, "cap_b", 2))
        # total 4 > 3 → the whole oldest collection (cap_a) is evicted.
        assert list(STORE.keys()) == ["cap_b"]
        assert len(STORE["cap_b"].items) == 2

    def test_just_loaded_collection_survives_even_over_cap(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_STORE_MAX_ITEMS", "1")
        STORE.clear()
        load_collection(_write_collection_file(tmp_path, "cap_big", 4))
        assert list(STORE.keys()) == ["cap_big"]

    def test_reload_moves_collection_to_back_of_eviction_queue(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_STORE_MAX_ITEMS", "2")
        STORE.clear()
        pa = _write_collection_file(tmp_path, "cap_a", 1)
        load_collection(pa)
        load_collection(_write_collection_file(tmp_path, "cap_b", 1))
        load_collection(pa)  # reload → queue order is now [cap_b, cap_a]
        load_collection(_write_collection_file(tmp_path, "cap_c", 1))
        # total 3 > 2 → cap_b (now oldest) is evicted, NOT the reloaded cap_a.
        assert list(STORE.keys()) == ["cap_a", "cap_c"]

    def test_invalid_value_treated_as_unlimited(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_STORE_MAX_ITEMS", "not-a-number")
        STORE.clear()
        load_collection(_write_collection_file(tmp_path, "cap_a", 2))
        load_collection(_write_collection_file(tmp_path, "cap_b", 2))
        assert list(STORE.keys()) == ["cap_a", "cap_b"]


# ── SecondBrainAdapter status cache + async default_run_cli ──


_SB_CLIENT: KBClient = {
    "name": "second_brain",
    "type": "vector",
    "provider": "second_brain",
    "config": {},
}

_SB_PARAMS: VectorSearchParams = {
    "run_id": "run-cap-001",
    "phase": "discovery",
    "query": "how is config resolved",
    "top_k": 5,
}

_SB_INDEX_PATH = "/data/vault/index"

_GOOD_STATUS = RunCliResult(
    code=0,
    stdout=json.dumps({"enabled": True, "index_path": _SB_INDEX_PATH}) + "\n",
    stderr="",
)

_GOOD_QUERY = RunCliResult(
    code=0,
    stdout=json.dumps({"status": "ok", "results": [{"path": "a"}]}) + "\n",
    stderr="",
)


class TestSecondBrainStatusCache:
    def test_status_spawned_once_across_searches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two searches on the same adapter → ONE status spawn, two queries."""
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", _SB_INDEX_PATH)
        calls: list[str] = []

        async def run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
            calls.append(args[0])
            return _GOOD_STATUS if args[0] == "status" else _GOOD_QUERY

        adapter = create_second_brain_adapter(run_cli)
        first = asyncio.run(adapter.vector_search(_SB_CLIENT, _SB_PARAMS))
        second = asyncio.run(adapter.vector_search(_SB_CLIENT, _SB_PARAMS))

        assert first["status"] == "ok"
        assert second == first
        assert calls == ["status", "query", "query"]

    def test_failed_status_probe_is_not_cached(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing probe degrades AND retries on the next call (no negative
        caching), then succeeds once the store comes up."""
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", _SB_INDEX_PATH)
        calls: list[str] = []
        broken = [True]

        async def run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
            calls.append(args[0])
            if args[0] == "status":
                if broken[0]:
                    return RunCliResult(code=2, stdout="", stderr="boom\n")
                return _GOOD_STATUS
            return _GOOD_QUERY

        adapter = create_second_brain_adapter(run_cli)
        down = asyncio.run(adapter.vector_search(_SB_CLIENT, _SB_PARAMS))
        assert down["status"] == "provider_not_configured"

        broken[0] = False
        up = asyncio.run(adapter.vector_search(_SB_CLIENT, _SB_PARAMS))
        assert up["status"] == "ok"
        assert calls == ["status", "status", "query"]

    def test_handshake_still_checked_per_call_against_live_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The cached status is re-compared to the CURRENT env every call, so
        an env change after caching still fails the handshake."""
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", _SB_INDEX_PATH)

        async def run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
            return _GOOD_STATUS if args[0] == "status" else _GOOD_QUERY

        adapter = create_second_brain_adapter(run_cli)
        assert (
            asyncio.run(adapter.vector_search(_SB_CLIENT, _SB_PARAMS))["status"]
            == "ok"
        )
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", "/some/other/path")
        moved = asyncio.run(adapter.vector_search(_SB_CLIENT, _SB_PARAMS))
        assert moved["status"] == "provider_not_configured"
        assert "mismatch" in moved["error"]


class TestDefaultRunCli:
    """Live-fire coverage of the asyncio subprocess runner (no second_brain
    venv exists on any test host, so failure paths are exercised for real)."""

    def test_missing_interpreter_maps_to_code_1_no_throw(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(
            "SECOND_BRAIN_PYTHON", str(tmp_path / "no" / "such" / "python")
        )
        result = asyncio.run(default_run_cli(["status", "--json"], 10_000))
        assert result == RunCliResult(code=1, stdout="", stderr="")

    def test_real_spawn_module_missing_nonzero_exit_captured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A real interpreter without the second_brain package exits non-zero;
        the runner captures it instead of raising."""
        monkeypatch.setenv("SECOND_BRAIN_PYTHON", sys.executable)
        monkeypatch.delenv("PYTHONPATH", raising=False)
        result = asyncio.run(default_run_cli(["status", "--json"], 60_000))
        assert result.code != 0
        assert "second_brain" in result.stderr

    def test_fake_module_success_captures_stdout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pkg = tmp_path / "second_brain"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "cli.py").write_text(
            'print(\'{"enabled": true}\')\n', encoding="utf-8"
        )
        monkeypatch.setenv("SECOND_BRAIN_PYTHON", sys.executable)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        result = asyncio.run(default_run_cli(["status", "--json"], 60_000))
        assert result.code == 0
        assert json.loads(result.stdout) == {"enabled": True}

    def test_timeout_kills_process_and_maps_to_code_1(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        pkg = tmp_path / "second_brain"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "cli.py").write_text(
            "import time\ntime.sleep(60)\n", encoding="utf-8"
        )
        monkeypatch.setenv("SECOND_BRAIN_PYTHON", sys.executable)
        monkeypatch.setenv("PYTHONPATH", str(tmp_path))
        started = time.monotonic()
        result = asyncio.run(default_run_cli(["status", "--json"], 2_000))
        elapsed = time.monotonic() - started
        assert result.code == 1
        assert elapsed < 30, "timeout must kill the child, not wait 60s"


# ── kb_tools BM25 loaded-index cache ──────────────────────────


def _make_ctx(base_dir: str) -> KBContext:
    """Non-active-run KBContext resolving ``base_dir`` (cf. test_kb_tools)."""
    return KBContext(
        get_project_root=lambda: base_dir,
        get_config_storage_base_dir=lambda: ".",
        get_active_run=lambda: None,
        get_active_run_dir=lambda: "/should/not/be/used",
        base_dir_from_run_dir=lambda _d: "/should/not/be/used",
    )


def _persist_index(base_dir: str, collection: str, items: list[ResearchItem]) -> None:
    idx = BM25Index()
    idx.build(items, collection)
    idx.persist(bm25_index_dir(base_dir, collection))


def _bm25_item(id: str, content: str) -> ResearchItem:
    return ResearchItem(
        id=id, title=f"Item {id}", content=content, tags=[], metadata={}
    )


class TestBM25IndexCache:
    def test_index_loaded_once_across_searches(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base_dir = str(tmp_path)
        _persist_index(base_dir, "c", [_bm25_item("i1", "alpha alpha")])
        ctx = _make_ctx(base_dir)

        loads: list[str] = []
        orig_load = BM25Index.load_index

        def spy(self: BM25Index, index_dir: str) -> None:
            loads.append(index_dir)
            orig_load(self, index_dir)

        monkeypatch.setattr(BM25Index, "load_index", spy)

        args: dict[str, object] = {"run_id": "r", "phase": "p", "query": "alpha"}
        first = asyncio.run(handle_kb_search(dict(args), ctx))
        second = asyncio.run(handle_kb_search(dict(args), ctx))

        assert len(loads) == 1, "second search must hit the cache"
        # Byte-identical results across the cached call.
        assert json.loads(second.json)["results"] == (
            json.loads(first.json)["results"]
        )
        assert json.loads(first.json)["results"][0]["item_id"] == "i1"

    def test_cache_invalidated_on_rebuild(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base_dir = str(tmp_path)
        _persist_index(base_dir, "c", [_bm25_item("old1", "alpha alpha")])
        ctx = _make_ctx(base_dir)

        loads: list[str] = []
        orig_load = BM25Index.load_index

        def spy(self: BM25Index, index_dir: str) -> None:
            loads.append(index_dir)
            orig_load(self, index_dir)

        monkeypatch.setattr(BM25Index, "load_index", spy)

        args: dict[str, object] = {"run_id": "r", "phase": "p", "query": "alpha"}
        first = asyncio.run(handle_kb_search(dict(args), ctx))
        assert json.loads(first.json)["results"][0]["item_id"] == "old1"

        # Rebuild the same collection with different content (different file
        # size → the (mtime_ns, size) signature misses deterministically).
        _persist_index(
            base_dir, "c", [_bm25_item("new_item_1", "alpha alpha alpha beta")]
        )
        second = asyncio.run(handle_kb_search(dict(args), ctx))
        assert json.loads(second.json)["results"][0]["item_id"] == "new_item_1"
        assert len(loads) == 2, "rebuild must invalidate the cached index"


# ── PIPELINE_MCP_FETCH_MAX_BYTES ──────────────────────────────


class TestFetchMaxBytes:
    def test_truncates_and_marks_over_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_FETCH_MAX_BYTES", "10")

        async def fake_text(url: str, headers: dict[str, str]) -> tuple[int, str]:
            return 200, "<html><body>" + "a" * 50 + "</body></html>"

        monkeypatch.setattr(ws, "_http_get_text", fake_text)
        results = asyncio.run(ws.fetch_and_extract(["https://big.example"]))

        big = results[0]
        content = big["content"]
        assert isinstance(content, str)
        assert len(content) == 10
        assert big["content_length"] == 10
        assert big["truncated"] is True

        # The marker flows into item metadata, appended AFTER the frozen keys.
        items, _runs = ws._build_web_items(results, None, {})
        meta = items[0]["metadata"]
        assert isinstance(meta, dict)
        assert list(meta.keys()) == [
            "search_query",
            "search_snippet",
            "fetch_status",
            "content_length",
            "truncated",
        ]
        assert meta["truncated"] is True

    def test_under_cap_shape_is_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Sub-cap pages carry NO ``truncated`` key anywhere (undefined-key-
        drop) — the default cap only touches pathological pages."""

        async def fake_text(url: str, headers: dict[str, str]) -> tuple[int, str]:
            return 200, "<html><body>small page</body></html>"

        monkeypatch.setattr(ws, "_http_get_text", fake_text)
        results = asyncio.run(ws.fetch_and_extract(["https://small.example"]))
        assert "truncated" not in results[0]
        assert list(results[0].keys()) == [
            "url",
            "title",
            "content",
            "status",
            "content_length",
        ]
        items, _runs = ws._build_web_items(results, None, {})
        meta = items[0]["metadata"]
        assert isinstance(meta, dict)
        assert list(meta.keys()) == [
            "search_query",
            "search_snippet",
            "fetch_status",
            "content_length",
        ]

    def test_cap_zero_disables_truncation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_FETCH_MAX_BYTES", "0")
        big_body = "b" * (ws.DEFAULT_FETCH_MAX_BYTES + 10)

        async def fake_text(url: str, headers: dict[str, str]) -> tuple[int, str]:
            return 200, big_body

        monkeypatch.setattr(ws, "_http_get_text", fake_text)
        results = asyncio.run(ws.fetch_and_extract(["https://huge.example"]))
        content = results[0]["content"]
        assert isinstance(content, str)
        assert len(content) == len(big_body)
        assert "truncated" not in results[0]

    def test_http_layer_stops_reading_past_the_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real ``_http_get_text`` streams and stops at the cap (via a
        MockTransport — no network)."""
        monkeypatch.setenv("PIPELINE_MCP_FETCH_MAX_BYTES", "5")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"hello world, far too long")

        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def client_factory(**kwargs: Any) -> httpx.AsyncClient:
            return real_client(transport=transport, **kwargs)

        # ``ws`` does a module-level ``import httpx``, so patching the shared
        # httpx module is what its ``_http_get_text`` observes.
        monkeypatch.setattr(httpx, "AsyncClient", client_factory)
        status, text = asyncio.run(ws._http_get_text("https://x.example", {}))
        assert status == 200
        assert text == "hello"


# ── PIPELINE_MCP_QUERY_LOG_MAX_ENTRIES ────────────────────────


class TestQueryLogMaxEntries:
    def test_unset_is_unlimited(self) -> None:
        for i in range(5):
            append_search_entry("qr", "qp", f"q{i}", i)
        assert len(get_query_log("qr")) == 5

    def test_cap_evicts_oldest_from_both_stores(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_QUERY_LOG_MAX_ENTRIES", "2")
        append_search_entry("qr", "p1", "q1", 1)
        append_search_entry("qr", "p1", "q2", 2)
        append_search_entry("qr", "p2", "q3", 3)

        run_log = get_query_log("qr")
        assert [e["query"] for e in run_log] == ["q2", "q3"]
        # The evicted q1 is gone from its phase index too.
        assert [e["query"] for e in get_query_log("qr", "p1")] == ["q2"]
        assert [e["query"] for e in get_query_log("qr", "p2")] == ["q3"]

        # Export output shape is unchanged (same frozen key order).
        log = export_query_log("qr", "p1")
        assert list(log.keys()) == ["run_id", "phase", "queries"]
        assert [e["query"] for e in log["queries"]] == ["q2"]

    def test_cap_is_scoped_per_run(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_QUERY_LOG_MAX_ENTRIES", "1")
        append_search_entry("run_a", "p", "qa", 1)
        append_search_entry("run_b", "p", "qb", 1)
        assert [e["query"] for e in get_query_log("run_a")] == ["qa"]
        assert [e["query"] for e in get_query_log("run_b")] == ["qb"]

    def test_emptied_phase_key_reads_as_empty_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PIPELINE_MCP_QUERY_LOG_MAX_ENTRIES", "1")
        append_search_entry("qr", "p1", "q1", 1)
        append_search_entry("qr", "p2", "q2", 2)  # evicts q1 → p1 emptied
        assert get_query_log("qr", "p1") == []
        assert kb_client._phase_index.get("qr:p1") is None
