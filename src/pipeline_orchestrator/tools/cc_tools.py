"""Concept-collector MCP tool bodies (port of ``legacy-node/cc-handlers.ts``).

The seven ``handle_cc_*`` handlers take a :class:`CCContext` dependency object
(the DI seam that ``server.py`` wires to the real collection/concept functions)
and return a :class:`~pipeline_orchestrator.models.HandlerResponse`. Most
outputs are **plain-text line blocks** (not JSON) — the exact strings are the
contract (R1 D1–D7) and are preserved byte-for-byte.

:func:`register_cc_tools` registers the seven ``pipeline_cc_*`` tools onto a
FastMCP instance with the spec'd ``_meta.max_result_chars`` (50000 read /
10000 mutate, §5.4) and the matching ``ToolAnnotations`` subset —
``pipeline_cc_load_collection`` carries **only** ``_meta`` and **no**
annotations (R1 D1/E1, spec §4.3). It is NOT called from anywhere yet; the
global ``register_tools`` seam (``tools/__init__.py``) stays a no-op until the
wiring task (T6.5).

Parity notes:

* **``max_content_length`` / ``full``.** ``handle_cc_get_items`` truncates
  content to 500 chars by default, appends ``...`` when truncated, honors a
  numeric ``max_content_length`` override, and returns full content when
  ``full is True`` (TS ``Infinity`` → :data:`math.inf` here).
* **``continuation_token``** decode = ``parseInt(Buffer.from(tok,
  'base64').toString('utf-8'), 10)`` →
  ``int(base64.b64decode(tok).decode('utf-8'))``.
* **``save_overview`` routing.** Prefers ``ctx.persist_overview`` (Feature-C
  run-scoped) when present, else falls back to ``ctx.save_overview`` (R1 D5).
"""

from __future__ import annotations

import base64
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from mcp.types import ToolAnnotations

from pipeline_orchestrator import collections_store, concepts
from pipeline_orchestrator.bm25 import BM25Index, bm25_index_dir
from pipeline_orchestrator.collections_store import ResearchItem
from pipeline_orchestrator.models import HandlerResponse, KnowledgeOverview
from pipeline_orchestrator.tools._envelope import tool_result

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


# ── Context provided by server.ts (the DI seam) ──────────────────────


class _CollectResultLike(Protocol):
    """Duck-typed shape ``ctx.collect_concepts`` returns.

    Mirrors the TS CCContext signature ``{ synthesis_prompt; chunk? }`` — the
    real engine returns a
    :class:`~pipeline_orchestrator.concepts.CollectResult`, but the handler only
    reads ``.synthesis_prompt`` and ``.chunk``.
    """

    @property
    def synthesis_prompt(self) -> str: ...

    @property
    def chunk(self) -> object | None: ...


@dataclass
class CCContext:
    """Dependency object the cc handlers call through (mirrors the TS ``CCContext``).

    Each field is a callable injected by ``server.py``. ``persist_overview`` /
    ``get_base_dir`` are the ``?:`` optionals (``None`` when a test fixture omits
    them); ``handle_cc_save_overview`` prefers ``persist_overview`` when present
    and otherwise falls back to ``save_overview``.
    """

    load_collection: Callable[..., object]
    query_items: Callable[..., list[ResearchItem]]
    get_items: Callable[[str, list[str]], list[ResearchItem]]
    collect_concepts: Callable[..., _CollectResultLike]
    save_overview: Callable[..., str]
    list_overviews: Callable[..., list[dict[str, object]]]
    get_overview: Callable[..., KnowledgeOverview | None]
    persist_overview: Callable[..., str] | None = None
    get_base_dir: Callable[[], str | None] | None = None


# ── Handlers ─────────────────────────────────────────────────────────


def handle_cc_load_collection(
    args: dict[str, object], ctx: CCContext
) -> HandlerResponse:
    """Load a collection; report field map + tags (TS ``handleCCLoadCollection``)."""
    file_path = args.get("file_path")
    if not file_path:
        raise ValueError("file_path is required")

    col = ctx.load_collection(
        file_path,
        args.get("name"),
        args.get("field_overrides"),
    )

    fm = col.field_map  # type: ignore[attr-defined]
    available_tags = col.available_tags  # type: ignore[attr-defined]
    lines = [
        f'Loaded collection "{col.name}"',  # type: ignore[attr-defined]
        f"  Items: {len(col.items)}",  # type: ignore[attr-defined]
        f"  Source: {col.file_path}",  # type: ignore[attr-defined]
        "  Field map:",
        f"    items_key: {fm.items_key}",
        f"    id: {fm.id_field}",
        f"    content: {fm.content_field}",
        f"    title: {fm.title_field}",
        f"    tags: {fm.tags_field}",
        f"    author: {fm.author_field}" if fm.author_field else "",
        f"    date: {fm.date_field}" if fm.date_field else "",
        f"    url: {fm.url_field}" if fm.url_field else "",
        "",
        f"  Available tags ({len(available_tags)}):",
        f"    {', '.join(available_tags)}",
    ]

    return HandlerResponse(json="\n".join(line for line in lines if line))


def handle_cc_query(args: dict[str, object], ctx: CCContext) -> HandlerResponse:
    """Query loaded items and render a preview block (TS ``handleCCQuery``)."""
    # Nullish-not-falsy limit (TS ``{ limit = 20 }`` defaults only on
    # ``undefined``). An absent ``limit`` must NOT be forwarded — passing an
    # explicit ``None`` into ``query_items(..., limit: int = 20)`` would defeat
    # its default and crash on a non-empty store (``int >= None``). An explicit
    # value (including ``0``) is forwarded unchanged.
    query_kwargs: dict[str, object] = {
        "collection": args.get("collection"),
        "tags": args.get("tags"),
        "search": args.get("search"),
        "fields": args.get("fields"),
    }
    limit = args.get("limit")
    if limit is not None:
        query_kwargs["limit"] = limit
    results = ctx.query_items(**query_kwargs)

    if len(results) == 0:
        return HandlerResponse(json="No items matched the query.")

    lines = [f"Found {len(results)} item(s):\n"]
    for item in results:
        preview = (
            item.content[:200] + "..." if len(item.content) > 200 else item.content
        )
        lines.append(f"[{item.id}] {item.title or '(untitled)'}")
        if item.author:
            lines.append(f"  Author: {item.author.name}")
        if len(item.tags) > 0:
            lines.append(f"  Tags: {', '.join(item.tags)}")
        lines.append(f"  {preview}")
        lines.append("")

    return HandlerResponse(json="\n".join(lines))


_CC_GET_ITEMS_DEFAULT_MAX_CONTENT_LENGTH = 500


def handle_cc_get_items(args: dict[str, object], ctx: CCContext) -> HandlerResponse:
    """Retrieve items by id, truncating content (TS ``handleCCGetItems``).

    Content is truncated to 500 chars by default (``...`` appended), honoring a
    numeric ``max_content_length`` override; ``full is True`` returns the full
    content regardless (TS ``Infinity``).
    """
    collection = args.get("collection")
    item_ids = args.get("item_ids")
    if not collection or not item_ids:
        raise ValueError("collection and item_ids are required")

    full = args.get("full") is True
    max_content_length_arg = args.get("max_content_length")
    if full:
        max_content_length: float = math.inf
    elif isinstance(max_content_length_arg, (int, float)) and not isinstance(
        max_content_length_arg, bool
    ):
        max_content_length = max_content_length_arg
    else:
        max_content_length = _CC_GET_ITEMS_DEFAULT_MAX_CONTENT_LENGTH

    assert isinstance(collection, str)
    assert isinstance(item_ids, list)
    items = ctx.get_items(collection, item_ids)

    if len(items) == 0:
        return HandlerResponse(
            json=f'No items found with the given IDs in "{collection}".'
        )

    lines: list[str] = []
    for item in items:
        lines.append(f"=== [{item.id}] {item.title or '(untitled)'} ===")
        if item.author:
            handle_suffix = f" (@{item.author.handle})" if item.author.handle else ""
            lines.append(f"Author: {item.author.name}{handle_suffix}")
        if item.date:
            lines.append(f"Date: {item.date}")
        if item.url:
            lines.append(f"URL: {item.url}")
        if len(item.tags) > 0:
            lines.append(f"Tags: {', '.join(item.tags)}")
        content = (
            item.content[: int(max_content_length)] + "..."
            if len(item.content) > max_content_length
            else item.content
        )
        lines.append("")
        lines.append(content)
        lines.append("")

    return HandlerResponse(json="\n".join(lines))


async def handle_cc_collect_concepts(
    args: dict[str, object], ctx: CCContext
) -> HandlerResponse:
    """Build the concept-synthesis prompt (TS ``handleCCCollectConcepts``).

    Three sub-paths: a BM25 retrieval path (when ``focus`` is set, total items
    > 15, and a built index exists for a collection), else the engine's chunked
    path (``continuation_token`` takes priority over a raw ``chunk_index``),
    else the raw prompt string.
    """
    title = args.get("title")
    item_groups = args.get("items")
    if not title or not item_groups:
        raise ValueError("title and items are required")
    assert isinstance(item_groups, list)

    source_groups = [
        {
            "collection": group["collection"],
            "items": ctx.get_items(group["collection"], group["item_ids"]),
        }
        for group in item_groups
    ]

    total_items = sum(len(g["items"]) for g in source_groups)
    if total_items == 0:
        raise ValueError("No items found for the given IDs")

    focus = args.get("focus")

    # BM25 retrieval path: focus + large collection + index exists.
    if focus and total_items > 15:
        assert isinstance(focus, str)
        base_dir = ctx.get_base_dir() if ctx.get_base_dir else None
        if base_dir:
            seen: set[str] = set()
            unique_collections: list[str] = []
            for g in item_groups:
                if g["collection"] not in seen:
                    seen.add(g["collection"])
                    unique_collections.append(g["collection"])

            for collection_name in unique_collections:
                index_dir = bm25_index_dir(base_dir, collection_name)
                idx = BM25Index()

                if idx.is_built(index_dir):
                    idx.load_index(index_dir)
                    top_k_arg = args.get("top_k")
                    top_k = (
                        top_k_arg
                        if isinstance(top_k_arg, int)
                        and not isinstance(top_k_arg, bool)
                        else 10
                    )
                    hits = idx.search(focus, top_k)
                    hit_ids = [h.item_id for h in hits]

                    filtered_groups = [
                        {
                            "collection": g["collection"],
                            "items": [
                                item for item in g["items"] if item.id in hit_ids
                            ],
                        }
                        for g in source_groups
                    ]
                    filtered_groups = [
                        g for g in filtered_groups if len(g["items"]) > 0
                    ]

                    selected_count = sum(len(g["items"]) for g in filtered_groups)

                    if len(filtered_groups) > 0 and selected_count > 0:
                        result = ctx.collect_concepts(
                            _to_source_groups(filtered_groups),
                            title=title,
                            focus=focus,
                            depth=args.get("depth"),
                        )
                        return HandlerResponse(
                            json=json.dumps(
                                {
                                    "synthesis_prompt": result.synthesis_prompt,
                                    "vector_retrieval": {
                                        "collection": collection_name,
                                        "total_items": total_items,
                                        "selected_items": selected_count,
                                        "hit_ids": hit_ids,
                                    },
                                },
                                indent=2,
                                ensure_ascii=False,
                            )
                        )

    # Fall through to existing chunked/full behavior.

    # Resolve chunk_index: continuation_token takes priority over raw chunk_index.
    chunk_index: int | None = None
    continuation_token = args.get("continuation_token")
    if isinstance(continuation_token, str):
        chunk_index = int(base64.b64decode(continuation_token).decode("utf-8"))
    else:
        raw_chunk_index = args.get("chunk_index")
        if isinstance(raw_chunk_index, int) and not isinstance(raw_chunk_index, bool):
            chunk_index = raw_chunk_index

    result = ctx.collect_concepts(
        _to_source_groups(source_groups),
        title=title,
        focus=focus,
        depth=args.get("depth"),
        chunk_index=chunk_index,
    )

    chunk = result.chunk
    if chunk:
        is_final = chunk.is_final_chunk  # type: ignore[attr-defined]
        response_obj: dict[str, object] = {"synthesis_prompt": result.synthesis_prompt}
        response_obj["chunk"] = _chunk_to_dict(chunk)
        response_obj["instruction"] = (
            "Final chunk. Merge concept results from all chunks into a single "
            "KnowledgeOverview."
            if is_final
            else (
                f"Chunk {chunk.chunk_index + 1}/{chunk.total_chunks}. "  # type: ignore[attr-defined]
                "After processing, call pipeline_cc_collect_concepts again with "
                "continuation_token "
                f'"{chunk.continuation_token}" to get the next chunk.'  # type: ignore[attr-defined]
            )
        )
        return HandlerResponse(
            json=json.dumps(response_obj, indent=2, ensure_ascii=False)
        )

    return HandlerResponse(json=result.synthesis_prompt)


def _to_source_groups(groups: list[dict[str, object]]) -> object:
    """Adapt the handler's ``[{collection, items}]`` dicts into engine ``SourceGroup``s.

    Imported lazily to avoid a module-import cycle (``concepts`` imports nothing
    from this module, but keeping the import local mirrors the lazy wiring style
    and keeps the handler usable with a mocked ``ctx.collect_concepts`` that
    ignores its first arg entirely).
    """
    from pipeline_orchestrator.concepts import SourceGroup  # noqa: PLC0415

    return [
        SourceGroup(collection=str(g["collection"]), items=g["items"])  # type: ignore[arg-type]
        for g in groups
    ]


def _chunk_to_dict(chunk: object) -> dict[str, object]:
    """Serialize a ``ChunkInfo`` into the TS chunk object shape (field order pinned).

    Mirrors the TS ``chunk`` object literal field order: ``chunk_index,
    total_chunks, item_count, total_items, is_final_chunk, continuation_token?``.
    The optional ``continuation_token`` is omitted when ``None`` (TS leaves it
    ``undefined``, which ``JSON.stringify`` drops).
    """
    out: dict[str, object] = {
        "chunk_index": chunk.chunk_index,  # type: ignore[attr-defined]
        "total_chunks": chunk.total_chunks,  # type: ignore[attr-defined]
        "item_count": chunk.item_count,  # type: ignore[attr-defined]
        "total_items": chunk.total_items,  # type: ignore[attr-defined]
        "is_final_chunk": chunk.is_final_chunk,  # type: ignore[attr-defined]
    }
    token = chunk.continuation_token  # type: ignore[attr-defined]
    if token is not None:
        out["continuation_token"] = token
    return out


def handle_cc_save_overview(args: dict[str, object], ctx: CCContext) -> HandlerResponse:
    """Mark an overview ``complete`` and persist it (TS ``handleCCSaveOverview``).

    Prefers ``ctx.persist_overview`` (Feature-C run-scoped) when present, else
    ``ctx.save_overview``; passes the ``output_dir`` override through.
    """
    overview = args.get("overview")
    if not overview:
        raise ValueError("overview is required")
    assert isinstance(overview, KnowledgeOverview)

    overview.status = "complete"
    output_dir_override = args.get("output_dir")
    if ctx.persist_overview is not None:
        file_path = ctx.persist_overview(overview, output_dir_override)
    else:
        file_path = ctx.save_overview(overview, output_dir_override)

    return HandlerResponse(
        json="\n".join(
            [
                f"Overview saved: {file_path}",
                f"  ID: {overview.overview_id}",
                f"  Title: {overview.title}",
                f"  Concepts: {len(overview.concepts)}",
                f"  Themes: {len(overview.themes)}",
            ]
        )
    )


def handle_cc_list_overviews(
    args: dict[str, object], ctx: CCContext
) -> HandlerResponse:
    """List saved overviews as a line block (TS ``handleCCListOverviews``)."""
    overviews = ctx.list_overviews(args.get("directory"))

    if len(overviews) == 0:
        return HandlerResponse(json="No saved overviews found.")

    lines = [f"{len(overviews)} overview(s):\n"]
    for o in overviews:
        lines.append(f"[{o['overview_id']}] {o['title']}")
        lines.append(
            f"  Status: {o['status']} | Concepts: {o['concept_count']} | "
            f"Sources: {o['source_count']}"
        )
        lines.append(f"  Created: {o['created_date']}")
        lines.append(f"  File: {o['file']}")
        lines.append("")

    return HandlerResponse(json="\n".join(lines))


def handle_cc_get_overview(args: dict[str, object], ctx: CCContext) -> HandlerResponse:
    """Fetch an overview as JSON, else not-found block (TS ``handleCCGetOverview``)."""
    overview_id = args.get("overview_id")
    if not overview_id:
        raise ValueError("overview_id is required")
    assert isinstance(overview_id, str)

    overview = ctx.get_overview(overview_id, args.get("directory"))
    if not overview:
        return HandlerResponse(
            json=f'Overview "{overview_id}" not found.', is_error=True
        )

    from pipeline_orchestrator.concepts import _overview_to_dict  # noqa: PLC0415

    return HandlerResponse(
        json=json.dumps(_overview_to_dict(overview), indent=2, ensure_ascii=False)
    )


# ── Tool registration (wired into the global register_tools seam) ───

_READ_MAX = 50000
_MUTATE_MAX = 10000


def register_cc_tools(mcp: FastMCP) -> None:
    """Register the seven ``pipeline_cc_*`` tools onto ``mcp`` (spec §5.4).

    Each tool carries ``_meta.max_result_chars`` (50000 read / 10000 mutate) and
    the matching ``ToolAnnotations`` subset. ``pipeline_cc_load_collection``
    carries **only** ``_meta`` and **no** annotations (R1 D1/E1, §4.3). The
    bodies are thin shells over the ``handle_cc_*`` handlers; the real DI
    context is wired by the global seam later (T6.5) — this function only makes
    the tools enumerate correctly on ``tools/list``.

    Wired into ``tools/__init__.py``: the global ``register_tools`` seam fans
    out to this registrar so the tools dispatch through their handlers (T6.7).
    """

    from pipeline_orchestrator.server import state  # noqa: PLC0415

    def _get_base_dir() -> str | None:
        try:
            return os.path.realpath(
                os.path.join(state.project_root(), state.config_storage_base_dir())
            )
        except Exception:  # noqa: BLE001 — legacy ccCtx.getBaseDir try/catch
            return None

    def _persist_overview(
        overview: KnowledgeOverview, output_dir_override: str | None = None
    ) -> str:
        legacy_base_dir = (
            state.base_dir_from_run_dir(state.active_run_dir)
            if state.active_run is not None
            else os.path.realpath(
                os.path.join(state.project_root(), state.config_storage_base_dir())
            )
        )
        run_data_dir = (
            state.active_run.run_data_dir if state.active_run is not None else None
        )
        return concepts.persist_overview(
            run_data_dir, legacy_base_dir, overview, output_dir_override
        )

    cc_ctx = CCContext(
        load_collection=collections_store.load_collection,
        query_items=collections_store.query_items,
        get_items=collections_store.get_items,
        collect_concepts=concepts.collect_concepts,
        save_overview=concepts.save_overview,
        list_overviews=concepts.list_overviews,
        get_overview=concepts.get_overview,
        persist_overview=_persist_overview,
        get_base_dir=_get_base_dir,
    )

    @mcp.tool(
        name="pipeline_cc_load_collection",
        description="Load a research collection from a JSON file into the store.",
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_cc_load_collection(
        file_path: str,
        name: str | None = None,
        field_overrides: dict[str, str] | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"file_path": file_path}
        if name is not None:
            args["name"] = name
        if field_overrides is not None:
            args["field_overrides"] = field_overrides
        return handle_cc_load_collection(args, cc_ctx)

    @mcp.tool(
        name="pipeline_cc_query",
        description="Query loaded research items by collection/tags/search/fields.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_cc_query(
        collection: str | None = None,
        tags: list[str] | None = None,
        search: str | None = None,
        fields: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {}
        if collection is not None:
            args["collection"] = collection
        if tags is not None:
            args["tags"] = tags
        if search is not None:
            args["search"] = search
        if fields is not None:
            args["fields"] = fields
        if limit is not None:
            args["limit"] = limit
        return handle_cc_query(args, cc_ctx)

    @mcp.tool(
        name="pipeline_cc_get_items",
        description="Retrieve specific research items by id from a loaded collection.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_cc_get_items(
        collection: str,
        item_ids: list[str],
        full: bool | None = None,
        max_content_length: int | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {
            "collection": collection,
            "item_ids": item_ids,
        }
        if full is not None:
            args["full"] = full
        if max_content_length is not None:
            args["max_content_length"] = max_content_length
        return handle_cc_get_items(args, cc_ctx)

    @mcp.tool(
        name="pipeline_cc_collect_concepts",
        description="Build a concept-extraction synthesis prompt from research items.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    async def pipeline_cc_collect_concepts(
        title: str,
        items: list[dict[str, object]],
        focus: str | None = None,
        depth: str | None = None,
        chunk_index: int | None = None,
        continuation_token: str | None = None,
        top_k: int | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"title": title, "items": items}
        if focus is not None:
            args["focus"] = focus
        if depth is not None:
            args["depth"] = depth
        if chunk_index is not None:
            args["chunk_index"] = chunk_index
        if continuation_token is not None:
            args["continuation_token"] = continuation_token
        if top_k is not None:
            args["top_k"] = top_k
        return await handle_cc_collect_concepts(args, cc_ctx)

    @mcp.tool(
        name="pipeline_cc_save_overview",
        description="Persist a completed KnowledgeOverview to disk.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    @tool_result
    def pipeline_cc_save_overview(
        overview: dict[str, object],
        output_dir: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"overview": overview}
        if output_dir is not None:
            args["output_dir"] = output_dir
        return handle_cc_save_overview(args, cc_ctx)

    @mcp.tool(
        name="pipeline_cc_list_overviews",
        description="List saved knowledge overviews with summary metadata.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_cc_list_overviews(
        directory: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {}
        if directory is not None:
            args["directory"] = directory
        return handle_cc_list_overviews(args, cc_ctx)

    @mcp.tool(
        name="pipeline_cc_get_overview",
        description="Retrieve a saved knowledge overview by id.",
        annotations=ToolAnnotations(readOnlyHint=True),
        meta={"max_result_chars": _READ_MAX},
    )
    @tool_result
    def pipeline_cc_get_overview(
        overview_id: str,
        directory: str | None = None,
    ) -> HandlerResponse:
        args: dict[str, object] = {"overview_id": overview_id}
        if directory is not None:
            args["directory"] = directory
        return handle_cc_get_overview(args, cc_ctx)
