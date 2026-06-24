"""Port of ``legacy-node/tests/kb-client.test.ts`` (31 cases).

Byte-exact behavioral parity for ``pipeline_orchestrator.kb_client``: the
:func:`create_kb_client` factory, the always-logging async :func:`vector_search`
/ :func:`sql_query`, and the in-memory query-log read/clear/export surface
(:func:`get_query_log`, :func:`clear_query_log`, :func:`export_query_log`).

The async ``vectorSearch`` / ``sqlQuery`` are awaited in the TS via the test
runner; here they are driven with ``asyncio.run(...)`` inside sync test bodies,
matching the existing handler-test house style (cf. ``test_ingest.py``). The
random nothing — there is no random output here — and the ISO ``timestamp`` is
never asserted as a literal, only structurally (``isinstance``/non-empty),
exactly as the Node suite does.

The 31 ported cases live in six classes mirroring the six ``describe`` blocks
(createKBClient 5, vectorSearch 6, sqlQuery 8, getQueryLog 5, clearQueryLog 2,
exportQueryLog 5). A clearly-separated final class holds a few additive
integrity checks (key order / undefined-drop) that the Node suite does not
assert but that pin the byte-exact wire shape this port must preserve.
"""

from __future__ import annotations

import asyncio

from pipeline_orchestrator.kb_client import (
    KBConfig,
    clear_query_log,
    create_kb_client,
    export_query_log,
    get_query_log,
    sql_query,
    vector_search,
)

# ── Fixtures ──────────────────────────────────────────────────

vector_config: KBConfig = {
    "name": "strategy-vectors",
    "type": "vector",
    "provider": "pinecone",
    "config": {"index": "kalshi-research", "dimensions": 1536},
}

sql_config: KBConfig = {
    "name": "market-db",
    "type": "sql",
    "provider": "postgres",
    "config": {"host": "localhost", "port": 5432, "database": "kalshi"},
    "sql_templates": {
        "get_contract_history": "SELECT * FROM contracts WHERE ticker = :ticker",
        "list_open_markets": "SELECT * FROM markets WHERE status = 'open'",
    },
}

chroma_config: KBConfig = {
    "name": "chroma-store",
    "type": "vector",
    "provider": "chroma",
    "config": {"collection": "research-docs"},
}

sqlite_config: KBConfig = {
    "name": "local-db",
    "type": "sql",
    "provider": "sqlite",
    "config": {"path": "/tmp/local.db"},
    "sql_templates": {
        "get_cached_prices": "SELECT * FROM prices WHERE date = :date",
    },
}


# ── createKBClient ────────────────────────────────────────────


class TestCreateKBClient:
    def test_creates_a_vector_client_from_config(self) -> None:
        client = create_kb_client(vector_config)
        assert client["name"] == "strategy-vectors"
        assert client["type"] == "vector"
        assert client["provider"] == "pinecone"
        assert client["config"] == vector_config["config"]

    def test_creates_a_sql_client_with_templates(self) -> None:
        client = create_kb_client(sql_config)
        assert client["name"] == "market-db"
        assert client["type"] == "sql"
        assert client["provider"] == "postgres"
        assert client["sql_templates"]
        assert "get_contract_history" in client["sql_templates"]
        assert "list_open_markets" in client["sql_templates"]

    def test_creates_a_chroma_vector_client(self) -> None:
        client = create_kb_client(chroma_config)
        assert client["provider"] == "chroma"
        assert client["type"] == "vector"

    def test_creates_a_sqlite_sql_client(self) -> None:
        client = create_kb_client(sqlite_config)
        assert client["provider"] == "sqlite"
        assert client["type"] == "sql"

    def test_preserves_sql_templates_as_undefined_for_vector_clients(
        self,
    ) -> None:
        client = create_kb_client(vector_config)
        assert "sql_templates" not in client


# ── vectorSearch ──────────────────────────────────────────────


class TestVectorSearch:
    def setup_method(self) -> None:
        clear_query_log()

    def test_returns_provider_not_configured_for_pinecone_stub(self) -> None:
        client = create_kb_client(vector_config)
        result = asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-001",
                    "phase": "discovery",
                    "query": "kalshi prediction market volume trends",
                    "top_k": 5,
                },
            )
        )

        assert result["status"] == "provider_not_configured"
        assert result["error"]
        assert "pinecone" in result["error"].lower()
        assert len(result["results"]) == 0
        assert result["results_count"] == 0

    def test_returns_provider_not_configured_for_chroma_stub(self) -> None:
        client = create_kb_client(chroma_config)
        result = asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-002",
                    "phase": "curation",
                    "query": "market sentiment analysis",
                    "top_k": 3,
                },
            )
        )

        assert result["status"] == "provider_not_configured"
        assert result["error"]
        assert "chroma" in result["error"].lower()
        assert result["results_count"] == 0

    def test_returns_error_when_called_on_a_sql_client(self) -> None:
        client = create_kb_client(sql_config)
        result = asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-003",
                    "phase": "discovery",
                    "query": "some query",
                    "top_k": 5,
                },
            )
        )

        assert result["status"] == "error"
        assert result["error"]
        assert "not a vector" in result["error"].lower()

    def test_logs_the_query_after_a_vector_search(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-log-001",
                    "phase": "synthesis",
                    "query": "election market liquidity",
                    "top_k": 10,
                },
            )
        )

        entries = get_query_log("run-log-001", "synthesis")
        assert len(entries) == 1
        assert entries[0]["source"] == "strategy-vectors"
        assert entries[0]["query_type"] == "vector_search"
        assert entries[0]["query"] == "election market liquidity"
        assert entries[0]["results_count"] == 0

    def test_logs_query_even_when_wrong_type(self) -> None:
        client = create_kb_client(sql_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-log-type-error",
                    "phase": "discovery",
                    "query": "query that fails",
                    "top_k": 5,
                },
            )
        )

        entries = get_query_log("run-log-type-error", "discovery")
        assert len(entries) == 1
        assert entries[0]["results_count"] == 0

    def test_accepts_optional_debate_transcript_id_and_top_k(self) -> None:
        client = create_kb_client(vector_config)
        result = asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-debate",
                    "phase": "debate",
                    "query": "contract YES probability",
                    "top_k": 3,
                    "debate_transcript_id": "debate-abc-123",
                },
            )
        )

        assert result["status"] == "provider_not_configured"
        assert result["results_count"] == 0


# ── sqlQuery ──────────────────────────────────────────────────


class TestSQLQuery:
    def setup_method(self) -> None:
        clear_query_log()

    def test_returns_provider_not_configured_for_postgres_stub(self) -> None:
        client = create_kb_client(sql_config)
        result = asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-001",
                    "phase": "synthesis",
                    "template_name": "get_contract_history",
                    "parameters": {"ticker": "KXBTC-23DEC31"},
                },
            )
        )

        assert result["status"] == "provider_not_configured"
        assert result["error"]
        assert "postgres" in result["error"].lower()
        assert len(result["rows"]) == 0
        assert result["rows_count"] == 0

    def test_returns_provider_not_configured_for_sqlite_stub(self) -> None:
        client = create_kb_client(sqlite_config)
        result = asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-002",
                    "phase": "curation",
                    "template_name": "get_cached_prices",
                    "parameters": {"date": "2026-01-15"},
                },
            )
        )

        assert result["status"] == "provider_not_configured"
        assert result["error"]
        assert "sqlite" in result["error"].lower()
        assert result["rows_count"] == 0

    def test_returns_error_for_unknown_template_name(self) -> None:
        client = create_kb_client(sql_config)
        result = asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-003",
                    "phase": "discovery",
                    "template_name": "nonexistent_template",
                    "parameters": {},
                },
            )
        )

        assert result["status"] == "error"
        assert result["error"]
        assert "nonexistent_template" in result["error"]

    def test_returns_error_when_called_on_a_vector_client(self) -> None:
        client = create_kb_client(vector_config)
        result = asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-004",
                    "phase": "discovery",
                    "template_name": "get_contract_history",
                    "parameters": {},
                },
            )
        )

        assert result["status"] == "error"
        assert result["error"]
        assert "not a sql" in result["error"].lower()

    def test_logs_the_query_after_a_sql_query(self) -> None:
        client = create_kb_client(sql_config)
        asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-log",
                    "phase": "synthesis",
                    "template_name": "list_open_markets",
                    "parameters": {},
                },
            )
        )

        entries = get_query_log("run-sql-log", "synthesis")
        assert len(entries) == 1
        assert entries[0]["source"] == "market-db"
        assert entries[0]["query_type"] == "sql_template"
        assert entries[0]["query"] == "list_open_markets"
        assert entries[0]["results_count"] == 0

    def test_logs_query_even_when_type_error(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-type-err",
                    "phase": "discovery",
                    "template_name": "some_template",
                    "parameters": {},
                },
            )
        )

        entries = get_query_log("run-sql-type-err", "discovery")
        assert len(entries) == 1
        assert entries[0]["results_count"] == 0

    def test_logs_query_even_when_template_unknown(self) -> None:
        client = create_kb_client(sql_config)
        asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-tmpl-err",
                    "phase": "curation",
                    "template_name": "bad_template",
                    "parameters": {"x": 1},
                },
            )
        )

        entries = get_query_log("run-sql-tmpl-err", "curation")
        assert len(entries) == 1
        assert entries[0]["parameters"] == {"x": 1}

    def test_includes_template_used_in_result_for_valid_template(self) -> None:
        client = create_kb_client(sql_config)
        result = asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "run-sql-tmpl-used",
                    "phase": "curation",
                    "template_name": "get_contract_history",
                    "parameters": {"ticker": "KXBTC"},
                },
            )
        )

        # Even though provider not configured, template was found.
        assert result["status"] == "provider_not_configured"
        assert result["template_used"]
        assert "contracts" in result["template_used"]


# ── getQueryLog ───────────────────────────────────────────────


class TestGetQueryLog:
    def setup_method(self) -> None:
        clear_query_log()

    def test_returns_all_entries_for_a_run_id_when_no_phase_filter(
        self,
    ) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-get-log",
                    "phase": "discovery",
                    "query": "q1",
                    "top_k": 5,
                },
            )
        )
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-get-log",
                    "phase": "synthesis",
                    "query": "q2",
                    "top_k": 5,
                },
            )
        )
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-get-log",
                    "phase": "synthesis",
                    "query": "q3",
                    "top_k": 3,
                },
            )
        )

        all_entries = get_query_log("run-get-log")
        assert len(all_entries) == 3

    def test_filters_by_phase_when_phase_is_provided(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-filter",
                    "phase": "discovery",
                    "query": "q1",
                    "top_k": 5,
                },
            )
        )
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-filter",
                    "phase": "synthesis",
                    "query": "q2",
                    "top_k": 5,
                },
            )
        )
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-filter",
                    "phase": "synthesis",
                    "query": "q3",
                    "top_k": 3,
                },
            )
        )

        synthesis = get_query_log("run-filter", "synthesis")
        assert len(synthesis) == 2

        discovery = get_query_log("run-filter", "discovery")
        assert len(discovery) == 1

    def test_returns_empty_array_for_unknown_run_id(self) -> None:
        entries = get_query_log("unknown-run-xyz")
        assert entries == []

    def test_returns_empty_array_for_known_run_but_unknown_phase(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-phase-miss",
                    "phase": "discovery",
                    "query": "q",
                    "top_k": 5,
                },
            )
        )

        entries = get_query_log("run-phase-miss", "nonexistent-phase")
        assert entries == []

    def test_each_log_entry_has_required_fields(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-fields",
                    "phase": "discovery",
                    "query": "field check query",
                    "top_k": 5,
                },
            )
        )

        entries = get_query_log("run-fields", "discovery")
        entry = entries[0]

        assert isinstance(entry["timestamp"], str)
        assert len(entry["timestamp"]) > 0
        assert isinstance(entry["source"], str)
        assert isinstance(entry["query_type"], str)
        assert isinstance(entry["query"], str)
        assert isinstance(entry["results_count"], int)
        assert isinstance(entry["results_used"], list)


# ── clearQueryLog ─────────────────────────────────────────────


class TestClearQueryLog:
    def test_removes_all_entries_across_all_runs(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {"run_id": "run-a", "phase": "discovery", "query": "q1", "top_k": 5},
            )
        )
        asyncio.run(
            vector_search(
                client,
                {"run_id": "run-b", "phase": "synthesis", "query": "q2", "top_k": 5},
            )
        )

        clear_query_log()

        assert get_query_log("run-a") == []
        assert get_query_log("run-b") == []

    def test_is_safe_to_call_on_an_already_empty_log(self) -> None:
        clear_query_log()
        clear_query_log()  # assert.doesNotThrow
        assert get_query_log("any-run") == []


# ── exportQueryLog ────────────────────────────────────────────


class TestExportQueryLog:
    def setup_method(self) -> None:
        clear_query_log()

    def test_exports_in_kb_query_log_schema_format(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-export",
                    "phase": "discovery",
                    "query": "kalshi volume trends",
                    "top_k": 5,
                },
            )
        )

        log = export_query_log("run-export", "discovery")
        assert log["run_id"] == "run-export"
        assert log["phase"] == "discovery"
        assert isinstance(log["queries"], list)
        assert len(log["queries"]) == 1

    def test_includes_all_queries_for_the_phase(self) -> None:
        client = create_kb_client(vector_config)
        sql_client = create_kb_client(sql_config)

        asyncio.run(
            vector_search(
                client,
                {"run_id": "run-exp2", "phase": "synthesis", "query": "q1", "top_k": 5},
            )
        )
        asyncio.run(
            sql_query(
                sql_client,
                {
                    "run_id": "run-exp2",
                    "phase": "synthesis",
                    "template_name": "get_contract_history",
                    "parameters": {"ticker": "X"},
                },
            )
        )
        asyncio.run(
            vector_search(
                client,
                {"run_id": "run-exp2", "phase": "discovery", "query": "q3", "top_k": 3},
            )
        )

        log = export_query_log("run-exp2", "synthesis")
        assert len(log["queries"]) == 2

    def test_exported_queries_have_required_schema_fields(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-exp3",
                    "phase": "curation",
                    "query": "test query for export",
                    "top_k": 5,
                },
            )
        )

        log = export_query_log("run-exp3", "curation")
        q = log["queries"][0]

        assert isinstance(q["timestamp"], str)
        assert isinstance(q["source"], str)
        assert isinstance(q["query_type"], str)
        assert isinstance(q["results_used"], list)

    def test_includes_debate_transcript_id_when_provided(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "run-debate-exp",
                    "phase": "debate",
                    "query": "debate query",
                    "top_k": 5,
                    "debate_transcript_id": "debate-xyz-999",
                },
            )
        )

        log = export_query_log("run-debate-exp", "debate", "debate-xyz-999")
        assert log["debate_transcript_id"] == "debate-xyz-999"
        assert len(log["queries"]) == 1

    def test_returns_empty_queries_array_for_unknown_run(self) -> None:
        log = export_query_log("no-such-run", "discovery")
        assert log["run_id"] == "no-such-run"
        assert log["phase"] == "discovery"
        assert log["queries"] == []


# ── Additive integrity checks (NOT in the Node suite) ─────────
#
# These pin the byte-exact wire shape (insertion-ordered keys + undefined-drop)
# that the port must preserve for downstream JSON serialization (T5.3). They are
# intentionally separate from the 31 ported cases above.


class TestWireShapeIntegrity:
    def setup_method(self) -> None:
        clear_query_log()

    def test_vector_entry_key_order_has_no_parameters_key(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {"run_id": "wire-vec", "phase": "p", "query": "q", "top_k": 5},
            )
        )
        entry = get_query_log("wire-vec", "p")[0]
        assert list(entry.keys()) == [
            "timestamp",
            "source",
            "query_type",
            "query",
            "results_count",
            "results_used",
        ]
        assert "parameters" not in entry
        assert "influence" not in entry

    def test_sql_entry_key_order_includes_parameters_between_query_and_count(
        self,
    ) -> None:
        client = create_kb_client(sql_config)
        asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "wire-sql",
                    "phase": "p",
                    "template_name": "list_open_markets",
                    "parameters": {"a": 1},
                },
            )
        )
        entry = get_query_log("wire-sql", "p")[0]
        assert list(entry.keys()) == [
            "timestamp",
            "source",
            "query_type",
            "query",
            "parameters",
            "results_count",
            "results_used",
        ]
        assert "influence" not in entry

    def test_export_key_order_appends_debate_id_after_queries(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {
                    "run_id": "wire-exp",
                    "phase": "p",
                    "query": "q",
                    "top_k": 5,
                    "debate_transcript_id": "d-1",
                },
            )
        )
        log = export_query_log("wire-exp", "p", "d-1")
        assert list(log.keys()) == [
            "run_id",
            "phase",
            "queries",
            "debate_transcript_id",
        ]

    def test_export_omits_debate_id_when_not_provided(self) -> None:
        log = export_query_log("wire-exp-none", "p")
        assert "debate_transcript_id" not in log
        assert list(log.keys()) == ["run_id", "phase", "queries"]

    def test_sql_stub_omits_template_used_when_no_templates(self) -> None:
        # A sql client with NO sql_templates skips template validation and the
        # stub omits ``template_used`` (template_sql is None).
        no_tmpl: KBConfig = {
            "name": "no-tmpl-db",
            "type": "sql",
            "provider": "postgres",
            "config": {},
        }
        client = create_kb_client(no_tmpl)
        result = asyncio.run(
            sql_query(
                client,
                {
                    "run_id": "wire-no-tmpl",
                    "phase": "p",
                    "template_name": "anything",
                    "parameters": {},
                },
            )
        )
        assert result["status"] == "provider_not_configured"
        assert "template_used" not in result

    def test_run_and_phase_share_the_same_entry_object(self) -> None:
        client = create_kb_client(vector_config)
        asyncio.run(
            vector_search(
                client,
                {"run_id": "wire-share", "phase": "p", "query": "q", "top_k": 5},
            )
        )
        run_entry = get_query_log("wire-share")[0]
        phase_entry = get_query_log("wire-share", "p")[0]
        assert run_entry is phase_entry
