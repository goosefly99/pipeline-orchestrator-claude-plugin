"""Knowledge-base MCP tool bodies (port of the KB handlers in
``legacy-node/misc-handlers.ts``).

Four ``handle_kb_*`` handlers — the WARM (BM25) + COLD (vector) search merge
point, a named-SQL-template query, the query-log export, and the BM25 index
build. They take a :class:`KBContext` dependency object (the DI seam that
``server.py`` wires to the real run-state/storage functions) where one is
needed, and return a :class:`~pipeline_orchestrator.models.HandlerResponse`.

:func:`register_kb_tools` registers the four ``pipeline_kb_*`` tools onto a
FastMCP instance with the spec'd ``_meta.max_result_chars`` (50000 read /
10000 mutate, §5.4) and the matching ``ToolAnnotations`` (all four carry
annotations — three ``readOnlyHint``, one ``destructiveHint``). It is NOT
wired into the global ``register_tools`` seam (``tools/__init__.py``); that
stays a no-op until the wiring task (T6.5).

Parity notes (docs/10 §9.3–§9.4, the frozen response shape):

* **Frozen merge response.** :func:`handle_kb_search` returns
  ``{status, results, results_count, vector_results, vector_status}`` in that
  exact key order. The vector leg **never mutates** the BM25
  ``results`` / ``results_count``.
* **Merged-hit dict shape + key order.** Each merged BM25 hit serializes to
  ``{item_id, score, metadata: {title, tags}, collection}`` — the TS
  ``{ ...hit, collection }`` spread, with ``collection`` appended last. The
  :class:`~pipeline_orchestrator.bm25.SearchHit` dataclass is converted to this
  dict explicitly.
* **Collection-subdir scan order.** The Node ``readdirSync`` enumerates
  collection subdirs in libuv code-point-sorted order; ``sorted(os.listdir(...))``
  reproduces it byte-exactly (UTF-8 byte order == Python str code-point order),
  as proved by T4.4. This is parity-critical for tied scores, where insertion
  order = collection-scan order × per-collection hit order.
* **Stable merge sort.** ``sorted(..., key=score, reverse=True)`` is stable and
  keeps insertion order for tied scores, matching V8's stable sort under the
  ``b.score - a.score`` comparator.
* **filter bridge.** The Node ``filter`` is ``{ tags?: string[] }`` passed
  straight into ``idx.search(query, topK, filter)``. The Python
  :meth:`BM25Index.search` takes ``filter_tags: list[str] | None``, so the
  handler extracts ``filter.get("tags")`` (when ``filter`` is a dict) and passes
  that.
* **Vector leg env gate.** ``SECOND_BRAIN_INDEX_PATH`` is read **directly** from
  ``os.environ`` (mirroring ``second_brain_adapter.py`` / the TS
  ``process.env.*``), NOT through the ``@cache``'d ``config.py`` readers — so
  env-toggling tests observe the live value. Unset → ``vector_status='disabled'``,
  ``vector_results=[]``, no adapter call. Set → ``create_kb_client(vector:
  second_brain)`` + ``await vector_search(...)``; ``status=='ok'`` →
  ``vector_results=result['results']`` + ``vector_status='ok'``; otherwise
  ``vector_results=[]`` + ``vector_status=result['status']``.
* **Required-arg guards.** A missing required arg raises ``ValueError`` (the
  sibling handlers' rendering of a TS ``throw new Error('x is required')``) with
  the byte-exact message.
* **JSON serialization.** Every ``json.dumps`` uses ``indent=2,
  ensure_ascii=False`` (matching Node ``JSON.stringify(obj, null, 2)`` raw UTF-8
  output), including the no-index error envelope.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp.types import ToolAnnotations

from pipeline_orchestrator.bm25 import BM25Index, SearchHit, bm25_index_dir
from pipeline_orchestrator.collections_store import query_items
from pipeline_orchestrator.kb_client import (
    KBConfig,
    SQLQueryParams,
    VectorSearchParams,
    append_search_entry,
    create_kb_client,
    export_query_log,
    sql_query,
    vector_search,
)
from pipeline_orchestrator.models import HandlerResponse

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP

    from pipeline_orchestrator.models import RunState


# ── Context provided by server.ts (the DI seam) ──────────────────────


@dataclass
class KBContext:
    """Dependency object the KB handlers call through (mirrors the TS
    ``KBBuildIndexContext``).

    Each field is a callable injected by ``server.py`` (typed as plain
    ``Callable`` like the ``CCContext`` sibling — the DI seam binds the real
    run-state/storage functions later, T6.5). The base-dir resolution used by
    :func:`handle_kb_search` and :func:`handle_kb_build_index` is
    ``base_dir_from_run_dir(get_active_run_dir())`` when ``get_active_run()`` is
    truthy, else
    ``abspath(join(get_project_root(), get_config_storage_base_dir()))`` (the TS
    ``resolve(...)``).
    """

    get_project_root: Callable[[], str]
    get_config_storage_base_dir: Callable[[], str]
    get_active_run: Callable[[], RunState | None]
    get_active_run_dir: Callable[[], str]
    base_dir_from_run_dir: Callable[[str], str]


def _resolve_base_dir(ctx: KBContext) -> str:
    """Resolve the artifact base dir (TS ``activeRun ? baseDirFromRunDir(...) :
    resolve(projectRoot, configStorageBaseDir)``)."""
    if ctx.get_active_run():
        return ctx.base_dir_from_run_dir(ctx.get_active_run_dir())
    return os.path.abspath(
        os.path.join(ctx.get_project_root(), ctx.get_config_storage_base_dir())
    )


# ── Handlers ─────────────────────────────────────────────────────────


async def handle_kb_search(
    args: dict[str, object], ctx: KBContext
) -> HandlerResponse:
    """Run a BM25 search merged across all built collection indexes, then an
    additive env-gated vector leg (TS ``handleKBSearch``; docs/10 §9.4).

    Steps: resolve base_dir → scan ``{base_dir}/kb/bm25/`` (missing →
    no-index error) → load each built collection index and ``search(query,
    top_k, filter_tags)`` tagging hits with their collection → merge, sort by
    score desc (stable), slice ``top_k`` → log the query → additive vector leg →
    return the frozen ``{status, results, results_count, vector_results,
    vector_status}`` shape. The vector leg never mutates ``results`` /
    ``results_count``.
    """
    run_id = args.get("run_id")
    phase = args.get("phase")
    query = args.get("query")
    top_k_arg = args.get("top_k")
    filter_arg = args.get("filter")
    if not run_id:
        raise ValueError("run_id is required")
    if not phase:
        raise ValueError("phase is required")
    if not query:
        raise ValueError("query is required")
    assert isinstance(run_id, str)
    assert isinstance(phase, str)
    assert isinstance(query, str)
    # Coerce ``top_k`` to int mirroring JS truncate-toward-zero. The tool
    # schema declares ``top_k: number`` so a conformant client may send a
    # float (e.g. ``3.0``); Node ``(args.top_k ?? 5)`` then ``.slice(0, topK)``
    # truncates toward zero. ``int(...)`` reproduces that: ``int(2.7)==2``,
    # ``int(3.0)==3``, ``int(0.0)==0``, ``int(True)==1`` (``bool`` is an ``int``
    # subclass, matching V8). The ``is not None`` nullish guard preserves an
    # explicit ``0``; the isinstance narrows ``object`` for mypy-strict.
    top_k: int
    if top_k_arg is None:
        top_k = 5
    elif isinstance(top_k_arg, (int, float)):
        top_k = int(top_k_arg)
    else:
        raise ValueError("top_k must be a number")

    filter_tags = filter_arg.get("tags") if isinstance(filter_arg, dict) else None

    # Resolve base dir.
    base_dir = _resolve_base_dir(ctx)

    # Scan for built indexes under kb/bm25/.
    kb_dir = os.path.join(base_dir, "kb", "bm25")
    if not os.path.exists(kb_dir):
        return HandlerResponse(
            json=json.dumps(
                {
                    "status": "error",
                    "results": [],
                    "results_count": 0,
                    "error": (
                        "No BM25 indexes found. Build one using "
                        "pipeline_kb_build_index first."
                    ),
                },
                indent=2,
                ensure_ascii=False,
            ),
            is_error=True,
        )

    # ``sorted(os.listdir(...))`` reproduces the libuv code-point scan order
    # (UTF-8 byte order == Python str code-point order) — parity-critical for
    # tied scores (T4.4).
    collections = [
        name
        for name in sorted(os.listdir(kb_dir))
        if os.path.isdir(os.path.join(kb_dir, name))
    ]

    # Collect ``(SearchHit, collection)`` pairs so the score sort stays
    # numerically typed; convert to the frozen dict shape only after slicing.
    all_hits: list[tuple[SearchHit, str]] = []
    for collection_name in collections:
        index_dir = bm25_index_dir(base_dir, collection_name)
        idx = BM25Index()
        if not idx.is_built(index_dir):
            continue
        idx.load_index(index_dir)
        hits = idx.search(query, top_k, filter_tags)
        for hit in hits:
            all_hits.append((hit, collection_name))

    # Sort merged results by score descending (stable), take top_k. Python's
    # ``sorted`` is stable, matching V8 under the ``b.score - a.score`` comparator.
    merged = sorted(all_hits, key=lambda pair: pair[0].score, reverse=True)
    sliced = merged[:top_k]

    # ``{ ...hit, collection }`` — collection appended last (frozen key order).
    results: list[dict[str, object]] = [
        {
            "item_id": hit.item_id,
            "score": hit.score,
            "metadata": {"title": hit.metadata.title, "tags": hit.metadata.tags},
            "collection": collection_name,
        }
        for hit, collection_name in sliced
    ]

    # Log the query.
    append_search_entry(run_id, phase, query, len(results))

    # ── Additive vector leg (SP3-P2) ────────────────────────────
    # Env-gated, never throws, never mutates the BM25 results/results_count
    # above. Unset → byte-identical to before: no adapter call, disabled.
    vector_results: list[object] = []
    vector_status = "disabled"
    if os.environ.get("SECOND_BRAIN_INDEX_PATH"):
        vector_config: KBConfig = {
            "name": "second_brain",
            "type": "vector",
            "provider": "second_brain",
            "config": {},
        }
        vector_client = create_kb_client(vector_config)
        vector_params: VectorSearchParams = {
            "run_id": run_id,
            "phase": phase,
            "query": query,
            "top_k": top_k,
        }
        if isinstance(filter_arg, dict):
            vector_params["filter"] = filter_arg
        vector_result = await vector_search(vector_client, vector_params)
        if vector_result["status"] == "ok":
            vector_results = list(vector_result["results"])
            vector_status = "ok"
        else:
            vector_results = []
            vector_status = vector_result["status"]

    return HandlerResponse(
        json=json.dumps(
            {
                "status": "ok",
                "results": results,
                "results_count": len(results),
                "vector_results": vector_results,
                "vector_status": vector_status,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


async def handle_kb_sql_query(args: dict[str, object]) -> HandlerResponse:
    """Run a named SQL template query against a KB client (TS ``handleKBSQLQuery``).

    Required-arg guards (run_id / phase / template_name / kb_config), ``parameters``
    defaulting to ``{}``, then ``create_kb_client(kb_config)`` +
    ``await sql_query(...)``. ``is_error`` mirrors ``result['status'] == 'error'``.
    """
    run_id = args.get("run_id")
    phase = args.get("phase")
    template_name = args.get("template_name")
    parameters_arg = args.get("parameters")
    parameters = parameters_arg if parameters_arg is not None else {}
    if not run_id:
        raise ValueError("run_id is required")
    if not phase:
        raise ValueError("phase is required")
    if not template_name:
        raise ValueError("template_name is required")
    kb_config_raw = args.get("kb_config")
    if not kb_config_raw:
        raise ValueError("kb_config is required")
    assert isinstance(run_id, str)
    assert isinstance(phase, str)
    assert isinstance(template_name, str)
    assert isinstance(parameters, dict)
    assert isinstance(kb_config_raw, dict)

    client = create_kb_client(kb_config_raw)  # type: ignore[arg-type]
    sql_params: SQLQueryParams = {
        "run_id": run_id,
        "phase": phase,
        "template_name": template_name,
        "parameters": parameters,
    }
    result = await sql_query(client, sql_params)
    return HandlerResponse(
        json=json.dumps(result, indent=2, ensure_ascii=False),
        is_error=result["status"] == "error",
    )


def handle_kb_export_query_log(args: dict[str, object]) -> HandlerResponse:
    """Export the KB query log for a run+phase (TS ``handleKBExportQueryLog``).

    Required-arg guards (run_id / phase); ``debate_transcript_id`` scopes the
    export when provided.
    """
    run_id = args.get("run_id")
    phase = args.get("phase")
    debate_transcript_id = args.get("debate_transcript_id")
    if not run_id:
        raise ValueError("run_id is required")
    if not phase:
        raise ValueError("phase is required")
    assert isinstance(run_id, str)
    assert isinstance(phase, str)
    transcript_id = (
        debate_transcript_id if isinstance(debate_transcript_id, str) else None
    )

    log = export_query_log(run_id, phase, transcript_id)
    return HandlerResponse(json=json.dumps(log, indent=2, ensure_ascii=False))


def handle_kb_build_index(
    args: dict[str, object], ctx: KBContext
) -> HandlerResponse:
    """Build a BM25 index from a loaded in-memory collection (TS
    ``handleKBBuildIndex``).

    Guards ``collection_name``; pulls every item via ``query_items(collection,
    limit=999999)`` (empty → raises with the byte-exact load-it message);
    resolves base_dir; builds + persists the index. Response keys in order:
    ``status, collection_name, index_path, item_count, avg_doc_length, next_step``.
    """
    collection_name = args.get("collection_name")
    if not collection_name:
        raise ValueError("collection_name is required")
    assert isinstance(collection_name, str)

    # Get all items from the loaded collection.
    items = query_items(collection=collection_name, limit=999999)
    if len(items) == 0:
        raise ValueError(
            f'Collection "{collection_name}" not found or empty. '
            "Load it with pipeline_cc_load_collection first."
        )

    # Resolve base dir.
    base_dir = _resolve_base_dir(ctx)

    # Build index.
    index_dir = bm25_index_dir(base_dir, collection_name)
    idx = BM25Index()
    metadata = idx.build(items, collection_name)
    idx.persist(index_dir)

    return HandlerResponse(
        json=json.dumps(
            {
                "status": "ok",
                "collection_name": collection_name,
                "index_path": index_dir,
                "item_count": metadata.item_count,
                "avg_doc_length": metadata.avg_doc_length,
                "next_step": (
                    "BM25 index built. Use pipeline_kb_search with query to "
                    "search."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


# ── Tool registration (NOT wired into the global seam yet — T6.5) ────

_READ_MAX = 50000
_MUTATE_MAX = 10000


def register_kb_tools(mcp: FastMCP) -> None:
    """Register the four ``pipeline_kb_*`` tools onto ``mcp`` (spec §5.4).

    ``pipeline_kb_search`` / ``pipeline_kb_sql_query`` /
    ``pipeline_kb_export_query_log`` carry ``ToolAnnotations(readOnlyHint=True)``
    + ``_meta.max_result_chars = 50000``; ``pipeline_kb_build_index`` carries
    ``ToolAnnotations(destructiveHint=True)`` + ``_meta.max_result_chars =
    10000``. The bodies are thin shells over the ``handle_kb_*`` handlers; the
    real DI context is wired by the global seam later (T6.5) — this function only
    makes the tools enumerate correctly on ``tools/list``.

    NOT called from ``tools/__init__.py`` yet: the global ``register_tools``
    handshake stays at its current count until the wiring task.
    """

    @mcp.tool(
        name="pipeline_kb_search",
        description=(
            "Run a BM25 ranked text search over an indexed collection for a "
            "given run/phase."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    async def pipeline_kb_search(
        run_id: str,
        phase: str,
        query: str,
        top_k: float | None = None,
        filter: dict[str, object] | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_kb_sql_query",
        description=(
            "Run a named SQL template query against the knowledge base for a "
            "given run/phase."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    async def pipeline_kb_sql_query(
        kb_config: dict[str, object],
        run_id: str,
        phase: str,
        template_name: str,
        parameters: dict[str, object] | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_kb_build_index",
        description=(
            "Build a BM25 text search index from a loaded in-memory collection."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_kb_build_index(
        collection_name: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_kb_export_query_log",
        description=(
            "Export the KB query log for a given run/phase, optionally scoped to "
            "a debate transcript."
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    def pipeline_kb_export_query_log(
        run_id: str,
        phase: str,
        debate_transcript_id: str | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")
