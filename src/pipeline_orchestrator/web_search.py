"""Web search + content fetching (port of ``legacy-node/web-search.ts``).

Byte-exact behavioral port of the Node ``web-search.ts`` module backing the
eventual ``pipeline_web_search`` tool (the FastMCP seam lands later in T6.4 —
this module registers nothing). Covers provider-dispatched search
(:func:`web_search` → Brave or DuckDuckGo), URL fetch + tag-strip
(:func:`fetch_and_extract`), RawItem/SourceRun construction
(:func:`_build_web_items`), the disk merge (:func:`merge_into_collection`), and
the two-mode orchestrator (:func:`execute_web_search`).

Parity notes:

* **Self-contained dict shapes.** Like ``ingest.ts``, this module emits plain,
  insertion-ordered ``dict`` values (mirroring the TS object-literal key order)
  rather than dataclasses — RawItem / SourceRun keep ``raw-collection.json``
  validity and keep ``json.dumps(collection, ...)`` byte-equal to the Node
  ``JSON.stringify(collection, ...)``. ``strip_html_tags`` / ``_now_iso`` /
  ``secrets.token_hex(4)`` are reused from :mod:`pipeline_orchestrator.ingest`.
* **Nullish-coalescing, not falsy.** Every TS ``?? default`` / ``=== undefined``
  becomes an explicit ``is None`` check or ``dict.get(k, default)`` — a
  present-but-falsy value (``""``, ``0``, ``False``, ``[]``) must survive. In
  particular ``PIPELINE_SEARCH_API=""`` (empty string) is *present*, so it
  falls through to the DuckDuckGo default branch rather than being re-defaulted;
  ``max_results_per_query=0`` survives; an empty ``search_results`` list still
  counts as "search mode was used".
* **Undefined-key-drop.** In the Node ``buildWebItems``, the SourceRun is built
  with ``query: searchQuery`` where ``searchQuery`` may be ``undefined``;
  ``JSON.stringify`` DROPS undefined-valued keys. So when ``search_query is
  None`` the ``query`` key is OMITTED entirely from the source_run dict (NOT
  emitted as ``"query": None`` → that would serialize to JSON ``null`` and
  diverge). ``SourceRun.query`` is optional in the interface.
* **No trailing newline on merge write.** Node ``JSON.stringify(collection, null,
  2)`` + ``writeFileSync`` add no trailing newline; :func:`merge_into_collection`
  writes ``json.dumps(..., indent=2, ensure_ascii=False)`` with none either, so
  the on-disk bytes match. ``ensure_ascii=False`` preserves raw UTF-8.
* **httpx async.** The TS ``fetch`` + ``AbortSignal.timeout(10_000)`` becomes an
  ``httpx.AsyncClient`` with a 10.0s timeout. All real I/O is funnelled through
  two tiny private helpers (:func:`_http_get_json`, :func:`_http_get_text`) so
  tests can monkeypatch them without network. ``httpx`` is an existing
  dependency (no new deps added).
* **MCP-stdio safety.** Nothing here writes to stdout at import or runtime.
"""

from __future__ import annotations

import json
import re
import secrets

import httpx

from .ingest import _now_iso, strip_html_tags

# ── Constants ────────────────────────────────────────────────

FETCH_TIMEOUT_MS = 10_000
_FETCH_TIMEOUT_S = FETCH_TIMEOUT_MS / 1000.0

#: Default cap on fetched page content (2 MiB). Override via
#: ``PIPELINE_MCP_FETCH_MAX_BYTES``; ``<= 0`` disables the cap.
DEFAULT_FETCH_MAX_BYTES = 2_097_152


def _fetch_max_bytes() -> int:
    """Resolve the fetch/content cap from ``PIPELINE_MCP_FETCH_MAX_BYTES``.

    Unset / unparseable → :data:`DEFAULT_FETCH_MAX_BYTES`; ``<= 0`` → ``0``
    (no cap). Read from ``os.environ`` on every call (module idiom — tests
    toggle the env per-test, cf. ``PIPELINE_SEARCH_API`` above).
    """
    import os

    raw = os.environ.get("PIPELINE_MCP_FETCH_MAX_BYTES")
    if not raw:
        return DEFAULT_FETCH_MAX_BYTES
    try:
        cap = int(raw)
    except ValueError:
        return DEFAULT_FETCH_MAX_BYTES
    return cap if cap > 0 else 0


# ── HTTP layer (mockable) ────────────────────────────────────


async def _http_get_json(
    url: str,
    params: dict[str, str],
    headers: dict[str, str],
) -> tuple[int, str, object]:
    """GET ``url`` and parse the body as JSON.

    Returns ``(status_code, reason_phrase, parsed_body)``. Mirrors the TS
    ``fetch(...).json()`` path; isolated so tests can monkeypatch it.
    """
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
        resp = await client.get(url, params=params, headers=headers)
        return resp.status_code, resp.reason_phrase, resp.json()


async def _http_get_text(url: str, headers: dict[str, str]) -> tuple[int, str]:
    """GET ``url`` and return ``(status_code, body_text)``.

    Mirrors the TS ``fetch(...).text()`` path; isolated for monkeypatching.
    Streams the body and stops reading past the ``PIPELINE_MCP_FETCH_MAX_BYTES``
    cap (default 2 MiB) so a pathological page cannot exhaust memory. Pages
    under the cap decode identically to the previous ``resp.text`` (same
    encoding resolution, same ``errors="replace"``).
    """
    max_bytes = _fetch_max_bytes()
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            chunks: list[bytes] = []
            received = 0
            async for chunk in resp.aiter_bytes():
                chunks.append(chunk)
                received += len(chunk)
                if max_bytes and received >= max_bytes:
                    break
            body = b"".join(chunks)
            if max_bytes:
                body = body[:max_bytes]
            encoding = resp.encoding or "utf-8"
            return resp.status_code, body.decode(encoding, errors="replace")


# ── Search ───────────────────────────────────────────────────


async def web_search(
    queries: list[str],
    max_results_per_query: int,
) -> list[dict[str, str]]:
    """Execute web searches via a configurable provider.

    Provider is selected by ``PIPELINE_SEARCH_API`` (TS ``?? 'duckduckgo'`` —
    only an *absent* key defaults; an empty-string value survives and falls to
    the DuckDuckGo branch). Returns the flattened list of SearchResult dicts
    (``title``/``url``/``snippet``).
    """
    import os

    provider = os.environ.get("PIPELINE_SEARCH_API", "duckduckgo")
    results: list[dict[str, str]] = []

    for query in queries:
        if provider == "brave":
            batch = await _search_brave(query, max_results_per_query)
        else:
            batch = await _search_duckduckgo(query, max_results_per_query)
        results.extend(batch)

    return results


async def _search_brave(query: str, max: int) -> list[dict[str, str]]:
    """Brave Search API provider (requires ``BRAVE_API_KEY``)."""
    import os

    api_key = os.environ.get("BRAVE_API_KEY")
    if not api_key:
        raise ValueError(
            "BRAVE_API_KEY env var required when PIPELINE_SEARCH_API=brave"
        )

    status, reason, body = await _http_get_json(
        "https://api.search.brave.com/res/v1/web/search",
        {"q": query, "count": str(max)},
        {"X-Subscription-Token": api_key, "Accept": "application/json"},
    )

    if not 200 <= status < 300:
        raise ValueError(f"Brave search failed: {status} {reason}")

    web = body.get("web") if isinstance(body, dict) else None
    raw_results = web.get("results") if isinstance(web, dict) else None
    if raw_results is None:
        raw_results = []

    out: list[dict[str, str]] = []
    for r in raw_results[:max]:
        out.append(
            {
                "title": r["title"],
                "url": r["url"],
                "snippet": r["description"],
            }
        )
    return out


async def _search_duckduckgo(query: str, max: int) -> list[dict[str, str]]:
    """DuckDuckGo Instant-Answer provider (no key required)."""
    status, reason, body = await _http_get_json(
        "https://api.duckduckgo.com/",
        {"q": query, "format": "json", "no_redirect": "1"},
        {},
    )

    if not 200 <= status < 300:
        raise ValueError(f"DuckDuckGo search failed: {status} {reason}")

    topics = body.get("RelatedTopics") if isinstance(body, dict) else None
    if topics is None:
        topics = []

    filtered: list[dict[str, object]] = [
        t
        for t in topics
        if isinstance(t, dict) and t.get("FirstURL") and t.get("Text")
    ]

    out: list[dict[str, str]] = []
    for t in filtered[:max]:
        text = t["Text"]
        assert isinstance(text, str)
        first_url = t["FirstURL"]
        assert isinstance(first_url, str)
        out.append(
            {
                "title": text[:120],
                "url": first_url,
                "snippet": text,
            }
        )
    return out


# ── Fetch and extract ────────────────────────────────────────


def _extract_html_title(html: str) -> str:
    """Extract the ``<title>`` content from HTML (TS ``extractHtmlTitle``)."""
    match = re.search(r"<title[^>]*>([\s\S]*?)</title>", html, re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


async def fetch_and_extract(urls: list[str]) -> list[dict[str, object]]:
    """Fetch ``urls`` and extract text content from HTML.

    Individual failures are recorded (``status=0``, ``error`` set) but never
    abort the batch — mirrors the TS per-URL try/catch.
    """
    results: list[dict[str, object]] = []
    max_bytes = _fetch_max_bytes()

    for url in urls:
        try:
            status, html = await _http_get_text(
                url, {"User-Agent": "PipelineMCP/1.0"}
            )
            title = _extract_html_title(html)
            content = strip_html_tags(html)
            # ponytail: the stored-content cap counts characters against the
            # byte knob (chars <= raw bytes for the ASCII-dominated HTML this
            # guards against); exact byte accounting isn't worth the slicing.
            # Switch to byte-slicing if stored HTML ever goes multibyte-heavy.
            truncated = bool(max_bytes) and len(content) > max_bytes
            if truncated:
                content = content[:max_bytes]
            result: dict[str, object] = {
                "url": url,
                "title": title or url,
                "content": content,
                "status": status,
                "content_length": len(content),
            }
            # Undefined-key-drop: the key appears ONLY when the cap fired, so
            # sub-cap pages keep the frozen shape byte-identically.
            if truncated:
                result["truncated"] = True
            results.append(result)
        except Exception as err:
            results.append(
                {
                    "url": url,
                    "title": url,
                    "content": "",
                    "status": 0,
                    "content_length": 0,
                    "error": str(err),
                }
            )

    return results


# ── Build items + merge into collection ──────────────────────


def _build_web_items(
    fetched: list[dict[str, object]],
    search_query: str | None,
    search_snippets: dict[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    """Build RawItem + SourceRun dicts from fetched web content.

    Skips entries that carried an ``error``. RawItem/SourceRun key order mirrors
    the TS object literals; the SourceRun ``query`` key is OMITTED when
    ``search_query`` is ``None`` (undefined-key-drop parity).
    """
    now = _now_iso()
    items: list[dict[str, object]] = []
    source_runs: list[dict[str, object]] = []

    for f in fetched:
        if f.get("error"):
            continue

        url = f["url"]
        assert isinstance(url, str)

        metadata: dict[str, object] = {
            "search_query": search_query if search_query is not None else "",
            "search_snippet": search_snippets.get(url, ""),
            "fetch_status": f["status"],
            "content_length": f["content_length"],
        }
        # Undefined-key-drop: record truncation only when the fetch cap fired
        # (sub-cap pages keep the frozen metadata shape byte-identically).
        if f.get("truncated"):
            metadata["truncated"] = True

        item: dict[str, object] = {
            "id": f"web_{secrets.token_hex(4)}",
            "source_type": "web",
            "source_ref": f"url:{url}",
            "title": f["title"],
            "content": f["content"],
            "url": url,
            "date": now,
            "tags": [],
            "metadata": metadata,
        }
        items.append(item)

        source_run: dict[str, object] = {"source_type": "web"}
        # Undefined-key-drop: omit ``query`` entirely when None (TS ``query:
        # searchQuery`` with ``searchQuery === undefined`` is dropped by
        # JSON.stringify), rather than emitting a JSON ``null``.
        if search_query is not None:
            source_run["query"] = search_query
        source_run["url"] = url
        source_run["executed_at"] = now
        source_run["items_found"] = 1
        source_run["items_stored"] = 1
        source_runs.append(source_run)

    return items, source_runs


def merge_into_collection(
    collection_path: str,
    new_items: list[dict[str, object]],
    new_source_runs: list[dict[str, object]],
) -> None:
    """Append items + source_runs to a raw-collection JSON file on disk.

    Reads the file, extends ``items`` and ``source_runs`` in place, and writes
    it back with ``json.dumps(..., indent=2, ensure_ascii=False)`` and NO
    trailing newline (byte-exact vs Node ``JSON.stringify(collection, null, 2)``
    + ``writeFileSync``).
    """
    with open(collection_path, encoding="utf-8") as fh:
        collection = json.load(fh)

    items = collection["items"]
    assert isinstance(items, list)
    items.extend(new_items)

    source_runs = collection["source_runs"]
    assert isinstance(source_runs, list)
    source_runs.extend(new_source_runs)

    with open(collection_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(collection, indent=2, ensure_ascii=False))


# ── Public orchestrator ──────────────────────────────────────


async def execute_web_search(
    params: dict[str, object],
) -> dict[str, object]:
    """Orchestrate the two-mode web search tool (TS ``executeWebSearch``).

    Returns ``{"data": <dict>, "next_step": <str>}``. Search mode runs queries;
    fetch mode fetches URLs, builds items, and merges them into the collection;
    a single call may do both (search results are appended to the fetch
    response). Raises if neither ``queries`` nor ``fetch_urls`` is provided.
    """
    collection_path = params["collection_path"]
    assert isinstance(collection_path, str)

    max_results = params.get("max_results_per_query")
    if max_results is None:
        max_results = 5
    assert isinstance(max_results, int)

    search_results: list[dict[str, str]] | None = None
    snippet_map: dict[str, str] = {}

    # ── Search mode ──
    queries = params.get("queries")
    if queries and len(queries) > 0:  # type: ignore[arg-type]
        assert isinstance(queries, list)
        search_results = await web_search(queries, max_results)
        for r in search_results:
            snippet_map[r["url"]] = r["snippet"]

    # ── Fetch mode ──
    fetch_urls = params.get("fetch_urls")
    if fetch_urls and len(fetch_urls) > 0:  # type: ignore[arg-type]
        assert isinstance(fetch_urls, list)
        fetched = await fetch_and_extract(fetch_urls)
        search_query: str | None = (
            queries[0] if queries else None  # type: ignore[index]
        )
        items, source_runs = _build_web_items(
            fetched, search_query, snippet_map
        )
        failed = [f for f in fetched if f.get("error")]

        merge_into_collection(collection_path, items, source_runs)

        data: dict[str, object] = {
            "items_added": len(items),
            "items_failed": len(failed),
            "errors": [f"{f['url']}: {f['error']}" for f in failed],
            "collection_path": collection_path,
            "fetched_urls": fetch_urls,
        }
        # Include search results if both modes were used in one call. Mirrors JS
        # ``searchResults ? {...} : {}`` truthiness on the *variable* — an EMPTY
        # list still counts as "provided" (it is not None) and IS appended.
        if search_results is not None:
            data["search_results"] = search_results
            data["result_count"] = len(search_results)

        return {
            "data": data,
            "next_step": (
                "Call pipeline_register_artifact to register the updated "
                "collection."
            ),
        }

    # ── Search-only mode ──
    if search_results is not None:
        return {
            "data": {
                "search_results": search_results,
                "result_count": len(search_results),
            },
            "next_step": (
                "Review results and call again with fetch_urls to ingest "
                "selected pages."
            ),
        }

    raise ValueError("Either queries or fetch_urls (or both) must be provided.")
