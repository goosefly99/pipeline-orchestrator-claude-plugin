"""Knowledge-base client + in-memory query log (port of ``legacy-node/kb-client.ts``).

Byte-exact behavioral port of the Node ``kb-client.ts`` module (binding spec
docs/10 §9.3). Implements the provider-adapter registry keyed ``"{type}:{provider}"``,
the two in-memory query-log stores (a per-``run_id`` store and a per-``run_id:phase``
index), the ``KBClient`` factory, and the always-logging ``vector_search`` /
``sql_query`` operations plus the query-log read/clear/export surface.

Parity notes:

* **Three module-level stores, plain dicts (not classes).** ``_query_log_store``
  keyed by ``run_id`` → list of entries; ``_phase_index`` keyed by
  ``f"{run_id}:{phase}"`` → list of entries; ``_adapter_registry`` keyed by
  ``f"{type}:{provider}"`` → adapter. :func:`_append_entry` appends the *same*
  entry object to BOTH the run store and the phase index (mirroring the Node
  ``Map`` pair), so ``get_query_log(run)`` and ``get_query_log(run, phase)`` both
  observe it.
* **Self-contained dict shapes.** Like ``ingest.ts`` / ``web-search.ts``, this
  module emits plain, insertion-ordered ``dict`` values (mirroring the TS object
  literal key order) rather than dataclasses — this keeps downstream
  ``json.dumps(...)`` byte-equal to the Node ``JSON.stringify(...)`` via the
  undefined-key-drop idiom. ``TypedDict`` (with ``total=False`` for optional
  keys) provides mypy-strict typing while the runtime value stays a plain dict.
  The KBQueryLogEntry key order is ``timestamp, source, query_type, query,
  [parameters], results_count, results_used`` — ``parameters`` is present only
  for SQL entries (between ``query`` and ``results_count``); the optional
  ``influence`` key is never emitted.
* **Nullish-coalescing, not falsy.** TS ``?? []`` / ``=== undefined`` become
  ``dict.get(k, [])`` and explicit ``is None`` / ``is not None`` checks — a
  present-but-empty list must survive. ``get_query_log`` returns ``[]`` for an
  unknown run or phase via ``.get(..., [])``.
* **Undefined-key-drop.** :func:`export_query_log` appends ``debate_transcript_id``
  only when provided (after ``queries`` → key order ``run_id, phase, queries,
  debate_transcript_id``); the SQL stub result includes ``template_used`` only
  when ``sql_templates`` is defined AND the named template resolved (i.e.
  ``template_sql`` is not None).
* **Template-validation guard.** Only raise the "template not found" error when
  ``sql_templates`` is present (a dict) AND the name is missing; a SQL client
  with no ``sql_templates`` skips the check entirely and falls through to the
  provider stub.
* **SQL is effectively a stub.** No SQL is executed; the schema is kept for
  contract stability. With no adapters registered (these tests register none),
  every path hits the provider-not-configured stub.
* **Async surface.** ``vector_search`` / ``sql_query`` are ``async def`` (TS
  ``async``). The logged ``results_count`` equals ``result["results_count"]`` /
  ``result["rows_count"]`` (0 for stubs). Every path — type-guard,
  template-not-found, success/stub — logs an entry before returning.
* **MCP-stdio safety.** Nothing here writes to stdout at import or runtime;
  diagnostics (none currently) would go to stderr.
"""

from __future__ import annotations

from typing import Literal, Protocol, TypedDict, runtime_checkable

from .ingest import _now_iso

# ── Wire-record shapes (TypedDict for typing; plain dict at runtime) ──

KBType = Literal["vector", "sql"]
QueryType = Literal["vector_search", "sql_template", "sql_raw"]


class KBConfig(TypedDict, total=False):
    """Config shape for :func:`create_kb_client` (TS ``KBConfig``)."""

    name: str
    type: KBType
    provider: str
    config: dict[str, object]
    sql_templates: dict[str, str]


class KBClient(TypedDict, total=False):
    """Resolved KB client (TS ``KBClient``)."""

    name: str
    type: KBType
    provider: str
    config: dict[str, object]
    sql_templates: dict[str, str]


class VectorSearchParams(TypedDict, total=False):
    """Params for :func:`vector_search` (TS ``VectorSearchParams``)."""

    run_id: str
    phase: str
    query: str
    top_k: int
    filter: dict[str, object]
    debate_transcript_id: str


class VectorSearchResult(TypedDict, total=False):
    """Result of :func:`vector_search` (TS ``VectorSearchResult``)."""

    status: Literal["ok", "provider_not_configured", "error"]
    results: list[object]
    results_count: int
    error: str


class SQLQueryParams(TypedDict, total=False):
    """Params for :func:`sql_query` (TS ``SQLQueryParams``)."""

    run_id: str
    phase: str
    template_name: str
    parameters: dict[str, object]
    debate_transcript_id: str


class SQLQueryResult(TypedDict, total=False):
    """Result of :func:`sql_query` (TS ``SQLQueryResult``)."""

    status: Literal["ok", "provider_not_configured", "error"]
    rows: list[object]
    rows_count: int
    error: str
    template_used: str
    sql_expanded: str


class KBQueryLogEntry(TypedDict, total=False):
    """A single query-log entry (TS ``KBQueryLogEntry``).

    Key order at runtime is insertion-ordered: ``timestamp, source, query_type,
    query, [parameters], results_count, results_used`` — ``parameters`` appears
    only for SQL entries; ``influence`` is never emitted.
    """

    timestamp: str
    source: str
    query_type: QueryType
    query: str
    parameters: dict[str, object]
    results_count: int
    results_used: list[str]
    influence: str


class KBQueryLog(TypedDict, total=False):
    """Exported query log (TS ``KBQueryLog``)."""

    run_id: str
    phase: str
    debate_transcript_id: str
    queries: list[KBQueryLogEntry]


# ── Provider adapter interface ───────────────────────────────


@runtime_checkable
class KBProviderAdapter(Protocol):
    """A registered real provider adapter (TS ``KBProviderAdapter``).

    ``vector_search`` / ``sql_query`` are optional async methods so a future
    adapter (e.g. ``vector:second_brain``, T5.2) can implement just one. Stored
    under ``f"{type}:{provider}"`` by :func:`register_adapter`.
    """

    type: KBType
    provider: str

    async def vector_search(
        self, client: KBClient, params: VectorSearchParams
    ) -> VectorSearchResult: ...

    async def sql_query(
        self, client: KBClient, params: SQLQueryParams
    ) -> SQLQueryResult: ...


# ── In-memory stores ─────────────────────────────────────────

#: Keyed by ``run_id`` — holds all entries for that run.
_query_log_store: dict[str, list[KBQueryLogEntry]] = {}

#: Keyed by ``"run_id:phase"`` — index for fast phase-filtered lookups.
_phase_index: dict[str, list[KBQueryLogEntry]] = {}

#: Keyed by ``"type:provider"`` — registered real provider adapters.
_adapter_registry: dict[str, KBProviderAdapter] = {}


# ── Internal helpers ─────────────────────────────────────────


def _append_entry(run_id: str, phase: str, entry: KBQueryLogEntry) -> None:
    """Append ``entry`` to both the run-level store and the phase index.

    The *same* entry object is shared between the two stores (mirroring the Node
    ``Map`` pair) so a run-level read and a phase-level read observe it.
    """
    # Append to run-level store.
    run_entries = _query_log_store.get(run_id)
    if run_entries is None:
        run_entries = []
        _query_log_store[run_id] = run_entries
    run_entries.append(entry)

    # Append to phase index.
    phase_key = f"{run_id}:{phase}"
    phase_entries = _phase_index.get(phase_key)
    if phase_entries is None:
        phase_entries = []
        _phase_index[phase_key] = phase_entries
    phase_entries.append(entry)


def append_search_entry(
    run_id: str, phase: str, query: str, results_count: int
) -> None:
    """Log a BM25 search query (called by ``handle_kb_search`` in misc-handlers).

    Logged as ``source='bm25', query_type='vector_search'`` with no
    ``parameters`` key.
    """
    entry: KBQueryLogEntry = {
        "timestamp": _now_iso(),
        "source": "bm25",
        "query_type": "vector_search",
        "query": query,
        "results_count": results_count,
        "results_used": [],
    }
    _append_entry(run_id, phase, entry)


# ── Public API ───────────────────────────────────────────────


def register_adapter(adapter: KBProviderAdapter) -> None:
    """Register a real provider adapter for future use.

    Stored under ``f"{adapter.type}:{adapter.provider}"``.
    """
    key = f"{adapter.type}:{adapter.provider}"
    _adapter_registry[key] = adapter


def create_kb_client(config: KBConfig) -> KBClient:
    """Create a :class:`KBClient` from a :class:`KBConfig`.

    ``sql_templates`` is carried through as-is. Undefined-drop parity: the Node
    oracle leaves the property off the object entirely for templateless clients
    (so ``JSON.stringify`` omits it) rather than emitting ``null``, so the key is
    set only when present (the test asserts it is absent for vector clients).
    """
    client: KBClient = {
        "name": config["name"],
        "type": config["type"],
        "provider": config["provider"],
        "config": config["config"],
    }
    sql_templates = config.get("sql_templates")
    if sql_templates is not None:
        client["sql_templates"] = sql_templates
    return client


async def vector_search(
    client: KBClient, params: VectorSearchParams
) -> VectorSearchResult:
    """Execute a vector similarity search against a KB client. Always logs.

    Mirrors the TS ``vectorSearch``: type-guards a non-vector client (logs +
    error), tries a registered ``vector:{provider}`` adapter, else returns the
    provider-not-configured stub. The logged ``results_count`` equals the
    result's ``results_count``.
    """
    run_id = params["run_id"]
    phase = params["phase"]
    query = params["query"]

    # Type guard: must be a vector KB.
    if client["type"] != "vector":
        entry: KBQueryLogEntry = {
            "timestamp": _now_iso(),
            "source": client["name"],
            "query_type": "vector_search",
            "query": query,
            "results_count": 0,
            "results_used": [],
        }
        _append_entry(run_id, phase, entry)
        return {
            "status": "error",
            "results": [],
            "results_count": 0,
            "error": (
                f'"{client["name"]}" is not a vector KB '
                f'(type: {client["type"]})'
            ),
        }

    result: VectorSearchResult

    # Try registered adapter.
    adapter_key = f"vector:{client['provider']}"
    adapter = _adapter_registry.get(adapter_key)
    if adapter is not None and hasattr(adapter, "vector_search"):
        result = await adapter.vector_search(client, params)
    else:
        # Stub fallback — provider not configured.
        result = {
            "status": "provider_not_configured",
            "results": [],
            "results_count": 0,
            "error": (
                f'Vector provider "{client["provider"]}" is not configured. '
                f"Register a KBProviderAdapter to enable real queries."
            ),
        }

    # Log the query.
    log_entry: KBQueryLogEntry = {
        "timestamp": _now_iso(),
        "source": client["name"],
        "query_type": "vector_search",
        "query": query,
        "results_count": result["results_count"],
        "results_used": [],
    }
    _append_entry(run_id, phase, log_entry)

    return result


async def sql_query(client: KBClient, params: SQLQueryParams) -> SQLQueryResult:
    """Execute a SQL template query against a KB client. Always logs.

    Mirrors the TS ``sqlQuery``: type-guards a non-sql client (logs + error),
    validates the template name only when ``sql_templates`` is present, tries a
    registered ``sql:{provider}`` adapter, else returns the
    provider-not-configured stub (with ``template_used`` only when the template
    resolved). The logged ``results_count`` equals the result's ``rows_count``.
    """
    run_id = params["run_id"]
    phase = params["phase"]
    template_name = params["template_name"]
    parameters = params["parameters"]

    # Type guard: must be a sql KB.
    if client["type"] != "sql":
        entry: KBQueryLogEntry = {
            "timestamp": _now_iso(),
            "source": client["name"],
            "query_type": "sql_template",
            "query": template_name,
            "parameters": parameters,
            "results_count": 0,
            "results_used": [],
        }
        _append_entry(run_id, phase, entry)
        return {
            "status": "error",
            "rows": [],
            "rows_count": 0,
            "error": (
                f'"{client["name"]}" is not a sql KB (type: {client["type"]})'
            ),
        }

    # Validate template exists (only if templates are defined).
    sql_templates = client.get("sql_templates")
    template_sql = (
        sql_templates.get(template_name) if sql_templates is not None else None
    )
    if sql_templates is not None and template_sql is None:
        entry = {
            "timestamp": _now_iso(),
            "source": client["name"],
            "query_type": "sql_template",
            "query": template_name,
            "parameters": parameters,
            "results_count": 0,
            "results_used": [],
        }
        _append_entry(run_id, phase, entry)
        return {
            "status": "error",
            "rows": [],
            "rows_count": 0,
            "error": (
                f'SQL template "{template_name}" not found in '
                f'client "{client["name"]}"'
            ),
        }

    result: SQLQueryResult

    # Try registered adapter.
    adapter_key = f"sql:{client['provider']}"
    adapter = _adapter_registry.get(adapter_key)
    if adapter is not None and hasattr(adapter, "sql_query"):
        result = await adapter.sql_query(client, params)
    else:
        # Stub fallback — provider not configured.
        result = {
            "status": "provider_not_configured",
            "rows": [],
            "rows_count": 0,
            "error": (
                f'SQL provider "{client["provider"]}" is not configured. '
                f"Register a KBProviderAdapter to enable real queries."
            ),
        }
        # ``template_used`` only when ``sql_templates`` is defined AND resolved.
        if template_sql is not None:
            result["template_used"] = template_sql

    # Log the query.
    log_entry: KBQueryLogEntry = {
        "timestamp": _now_iso(),
        "source": client["name"],
        "query_type": "sql_template",
        "query": template_name,
        "parameters": parameters,
        "results_count": result["rows_count"],
        "results_used": [],
    }
    _append_entry(run_id, phase, log_entry)

    return result


def get_query_log(
    run_id: str, phase: str | None = None
) -> list[KBQueryLogEntry]:
    """Retrieve query-log entries for a run, optionally filtered by phase.

    Returns ``[]`` for an unknown ``run_id`` or unknown phase (TS ``?? []``).
    """
    if phase is not None:
        return _phase_index.get(f"{run_id}:{phase}", [])
    return _query_log_store.get(run_id, [])


def clear_query_log() -> None:
    """Clear all query-log entries from all in-memory stores."""
    _query_log_store.clear()
    _phase_index.clear()


def export_query_log(
    run_id: str, phase: str, debate_transcript_id: str | None = None
) -> KBQueryLog:
    """Export the query log for a run+phase in the kb-query-log schema format.

    ``debate_transcript_id`` is appended (after ``queries``) only when provided
    — mirroring the TS undefined-key-drop so the on-disk JSON omits the key
    otherwise. Key order: ``run_id, phase, queries, [debate_transcript_id]``.
    """
    queries = get_query_log(run_id, phase)
    log: KBQueryLog = {"run_id": run_id, "phase": phase, "queries": queries}
    if debate_transcript_id is not None:
        log["debate_transcript_id"] = debate_transcript_id
    return log
