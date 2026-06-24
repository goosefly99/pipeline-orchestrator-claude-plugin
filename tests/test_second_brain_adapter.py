"""Port of ``legacy-node/tests/second-brain-adapter.test.ts`` (12 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.second_brain_adapter``:
the :func:`create_second_brain_adapter` factory, the adapter shape
(``type`` / ``provider`` / callable ``vector_search``), and the read-only
``status`` → handshake → ``query`` flow that degrades every failure to
``provider_not_configured`` and NEVER issues ``build`` / ``rebuild``.

The async ``vector_search`` is driven with ``asyncio.run(...)`` inside sync test
bodies — matching the house style of ``test_kb_client.py`` (the suite does not
depend on ``pytest-asyncio``). The Node ``beforeEach`` that deletes the two env
vars is mirrored per-test with the ``monkeypatch`` fixture (function-scoped,
auto-restoring), so no env state leaks between tests.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from pipeline_orchestrator.kb_client import (
    KBClient,
    VectorSearchParams,
    VectorSearchResult,
)
from pipeline_orchestrator.second_brain_adapter import (
    RunCli,
    RunCliResult,
    create_second_brain_adapter,
)

# ── Fixtures ──────────────────────────────────────────────────

FIXTURE_INDEX_PATH = "/data/vault/index"
FIXTURE_EMBEDDER = "sentence-transformers/all-MiniLM-L6-v2@hf-fp32"

client: KBClient = {
    "name": "second_brain",
    "type": "vector",
    "provider": "second_brain",
    "config": {},
}

params: VectorSearchParams = {
    "run_id": "run-sb-001",
    "phase": "discovery",
    "query": "how is config resolved",
    "top_k": 5,
}


def json_line(obj: object) -> str:
    return json.dumps(obj) + "\n"


def _search(run_cli: RunCli) -> VectorSearchResult:
    """Drive the adapter's async ``vector_search`` to completion (house style:
    ``asyncio.run(...)`` in a sync body, cf. ``test_kb_client.py``)."""
    adapter = create_second_brain_adapter(run_cli)
    return asyncio.run(adapter.vector_search(client, params))


def make_fake_run_cli(
    status: RunCliResult | None = None,
    query: RunCliResult | None = None,
) -> tuple[RunCli, list[list[str]]]:
    """Build a fake ``run_cli`` that records every invocation's args and returns
    a scripted :class:`RunCliResult` keyed by subcommand (``status`` / ``query``).

    No real process is ever spawned — there is NO second_brain venv on this host.
    Any other subcommand (e.g. ``build`` / ``rebuild``) is a hard test failure —
    the adapter must NEVER issue one.
    """
    calls: list[list[str]] = []

    async def run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
        calls.append(args)
        sub = args[0]
        if sub == "status" and status is not None:
            return status
        if sub == "query" and query is not None:
            return query
        raise AssertionError(f"unexpected CLI subcommand in fake: {sub}")

    return run_cli, calls


@pytest.fixture(autouse=True)
def _clear_sb_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror the Node ``beforeEach``: delete both env vars the adapter reads so
    tests don't leak state. ``monkeypatch`` auto-restores after each test.
    """
    monkeypatch.delenv("SECOND_BRAIN_INDEX_PATH", raising=False)
    monkeypatch.delenv("SECOND_BRAIN_EMBEDDER_VERSION", raising=False)


# ── Adapter shape ─────────────────────────────────────────────


class TestCreateSecondBrainAdapter:
    def test_declares_the_vector_second_brain_provider_key(self) -> None:
        async def _noop(args: list[str], timeout_ms: int) -> RunCliResult:
            return RunCliResult(code=0, stdout="", stderr="")

        adapter = create_second_brain_adapter(_noop)
        assert adapter.type == "vector"
        assert adapter.provider == "second_brain"
        assert callable(adapter.vector_search)


# ── vector_search contract ────────────────────────────────────


class TestSecondBrainAdapterVectorSearch:
    def test_handshake_match_ok_maps_results_status_then_query_never_build(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        hits = [
            {
                "path": "/data/vault/wiki/a.md",
                "title": "A",
                "score": 0.91,
                "snippet": "alpha",
            },
            {
                "path": "/data/vault/wiki/b.md",
                "title": "B",
                "score": 0.77,
                "snippet": "beta",
            },
        ]
        run_cli, calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "backend": "sentence-transformers",
                        "enabled": True,
                        "embedder_version": FIXTURE_EMBEDDER,
                        "dimension": 384,
                        "count": 42,
                        "index_path": FIXTURE_INDEX_PATH,
                        "last_build": "2026-06-20T00:00:00Z",
                        "stale": False,
                    }
                ),
                stderr="loading model...\n",
            ),
            query=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "status": "ok",
                        "results": hits,
                        "embedder_version": FIXTURE_EMBEDDER,
                    }
                ),
                stderr="",
            ),
        )
        result = _search(run_cli)

        assert result["status"] == "ok"
        assert result["results_count"] == 2
        assert result["results"] == hits

        # status issued first, then query — and NOTHING else.
        assert len(calls) == 2
        assert calls[0][0] == "status"
        assert calls[1][0] == "query"
        assert calls[1][1] == params["query"]  # positional query text
        assert "--top-k" in calls[1]
        assert "5" in calls[1]
        all_args = [arg for call in calls for arg in call]
        assert "build" not in all_args, "must never issue build"
        assert "rebuild" not in all_args, "must never issue rebuild"

    def test_status_empty_ok_with_zero_results(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, _calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "enabled": True,
                        "index_path": FIXTURE_INDEX_PATH,
                        "embedder_version": FIXTURE_EMBEDDER,
                    }
                ),
                stderr="",
            ),
            query=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "status": "empty",
                        "results": [],
                        "embedder_version": FIXTURE_EMBEDDER,
                    }
                ),
                stderr="",
            ),
        )
        result = _search(run_cli)
        assert result["status"] == "ok"
        assert result["results_count"] == 0
        assert result["results"] == []

    def test_index_path_mismatch_not_configured_no_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "enabled": True,
                        "index_path": "/some/other/path",
                        "embedder_version": FIXTURE_EMBEDDER,
                    }
                ),
                stderr="",
            ),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert result["results_count"] == 0
        assert "mismatch" in result["error"]
        # Only status was issued — no query after a failed handshake.
        assert len(calls) == 1
        assert calls[0][0] == "status"

    def test_embedder_version_mismatch_env_set_not_configured_no_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        monkeypatch.setenv("SECOND_BRAIN_EMBEDDER_VERSION", FIXTURE_EMBEDDER)
        run_cli, calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "enabled": True,
                        "index_path": FIXTURE_INDEX_PATH,
                        "embedder_version": "some-other-embedder@v2",
                    }
                ),
                stderr="",
            ),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert "embedder_version" in result["error"]
        assert len(calls) == 1
        assert calls[0][0] == "status"

    def test_query_status_unavailable_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, _calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "enabled": True,
                        "index_path": FIXTURE_INDEX_PATH,
                        "embedder_version": FIXTURE_EMBEDDER,
                    }
                ),
                stderr="",
            ),
            query=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "status": "unavailable",
                        "reason": "index not built",
                        "results": [],
                    }
                ),
                stderr="",
            ),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert result["results_count"] == 0
        assert "unavailable" in result["error"]

    def test_enabled_not_true_not_configured_no_query(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line({"enabled": False, "index_path": FIXTURE_INDEX_PATH}),
                stderr="",
            ),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert len(calls) == 1

    def test_non_zero_exit_on_status_not_configured_no_throw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, calls = make_fake_run_cli(
            status=RunCliResult(code=2, stdout="", stderr="Traceback...\n"),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert result["results_count"] == 0
        assert len(calls) == 1

    def test_garbage_stdout_on_status_not_configured_no_throw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, _calls = make_fake_run_cli(
            status=RunCliResult(code=0, stdout="not json at all <<<\n", stderr=""),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert result["results_count"] == 0

    def test_garbage_stdout_on_query_not_configured_no_throw(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)
        run_cli, _calls = make_fake_run_cli(
            status=RunCliResult(
                code=0,
                stdout=json_line(
                    {
                        "enabled": True,
                        "index_path": FIXTURE_INDEX_PATH,
                        "embedder_version": FIXTURE_EMBEDDER,
                    }
                ),
                stderr="",
            ),
            query=RunCliResult(code=0, stdout="!!! totally broken", stderr=""),
        )
        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"

    def test_run_cli_that_throws_not_configured_never_throws_out(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SECOND_BRAIN_INDEX_PATH", FIXTURE_INDEX_PATH)

        async def run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
            raise RuntimeError("spawn ENOENT")

        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert "adapter error" in result["error"]

    def test_env_unset_not_configured_without_spawning_anything(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # SECOND_BRAIN_INDEX_PATH is deleted by the autouse fixture.
        spawned = False

        async def run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
            nonlocal spawned
            spawned = True
            return RunCliResult(code=0, stdout="", stderr="")

        result = _search(run_cli)
        assert result["status"] == "provider_not_configured"
        assert result["error"] == "SECOND_BRAIN_INDEX_PATH unset"
        assert spawned is False, "must not spawn when env is unset"
