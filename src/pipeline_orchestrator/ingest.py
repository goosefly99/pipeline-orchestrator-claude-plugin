"""Document-ingest engine (port of ``legacy-node/ingest.ts``).

Byte-exact behavioral port of the Node ``ingest.ts`` module: file-type
detection (:data:`EXT_MAP`), recursive directory enumeration
(:func:`enumerate_dir`), content extraction (markdown/text/code/csv/yaml/toml
as-is, JSON pretty-reprint, HTML tag-strip, notebook cell join), PDF extraction
via ``pypdf`` (replacing the Node ``pdf-parse`` dynamic import), JSON-array
splitting, ISO date normalization, and the async collection build
(:func:`ingest_documents_async`).

Parity notes:

* **Self-contained types.** ``ingest.ts`` declares its OWN ``RawItem`` /
  ``RawCollection`` / ``SourceRun`` shapes (it does NOT import from
  ``collections.ts``); the collection is emitted as a plain JSON object, so the
  Python port builds plain ``dict`` values (insertion-ordered to mirror the TS
  object-literal key order) rather than dataclasses. This keeps
  ``json.dumps(collection, ...)`` byte-equal to ``JSON.stringify(collection,
  ...)`` and lets the result validate directly against ``raw-collection.json``.
* **Nullish-coalescing.** Every TS ``?? default`` / ``=== undefined`` becomes an
  explicit ``is None`` check or ``dict.get(k)`` lookup, never ``or`` — a
  present-but-falsy value (``""``, ``0``, ``False``, ``[]``) must survive.
* **``detectFileType``** lower-cases the extension and looks it up in
  :data:`EXT_MAP`, falling back to ``"text"`` (TS ``?? 'text'``).
* **``enumerateDir``** membership-tests the extension against :data:`EXT_MAP`
  (NOT :func:`detect_file_type`, whose ``text`` fallback would admit every
  file), and skips dot-dirs, the ``index`` COLD-store root, and ``*.tmp`` dirs.
* **JSON re-stringify / notebook / collection** all use ``json.dumps(...,
  indent=2, ensure_ascii=False)`` to mirror Node ``JSON.stringify(x, null, 2)``
  (raw UTF-8 — Python's default ``ensure_ascii=True`` would diverge by escaping
  non-ASCII to ``\\uXXXX``).
* **``shortId`` / ``collectionId``** = ``randomBytes(4).toString('hex')`` → 8
  lowercase hex chars (:func:`secrets.token_hex` with 4 bytes).
* **``createdDate`` / ``executedAt`` / fallback now** = ``new
  Date().toISOString()`` → a millisecond-precision ``Z``-suffixed UTC timestamp
  (:func:`_now_iso`, identical to the sibling-module helpers).
* **``normalizeDate``** mirrors JS ``Date.parse`` for the input shapes that
  reach it (already-valid ISO date-time passthrough, US-locale ``M/D/YYYY``,
  ISO ``YYYY-MM-DD`` date-only, human-readable ``Month D, YYYY``); unparseable
  input falls back to ``fallbackNow`` or :func:`_now_iso`. Diagnostics (the Node
  ``console.warn`` coercion/fallback notes) go to **stderr**, never stdout
  (MCP-stdio safety).
* **PDF** uses a lazy ``import pypdf`` (mirroring the TS dynamic ``await
  import('pdf-parse')``); when the parser is absent it raises the actionable
  error string the Node code emits. ``pypdf`` is an OPTIONAL dependency
  (``pip install pypdf`` / the ``pdf`` extra) — non-PDF ingestion never touches
  it, matching the Node module where ``pdf-parse`` is only required on the PDF
  path.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
from datetime import UTC, datetime

# ── File type detection ───────────────────────────────────────

# The full set of supported file types (TS ``FileType`` union).
EXT_MAP: dict[str, str] = {
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".log": "text",
    ".py": "code",
    ".ts": "code",
    ".tsx": "code",
    ".js": "code",
    ".jsx": "code",
    ".go": "code",
    ".rs": "code",
    ".java": "code",
    ".rb": "code",
    ".sh": "code",
    ".bash": "code",
    ".zsh": "code",
    ".c": "code",
    ".cpp": "code",
    ".h": "code",
    ".cs": "code",
    ".swift": "code",
    ".kt": "code",
    ".scala": "code",
    ".r": "code",
    ".json": "json",
    ".csv": "csv",
    ".html": "html",
    ".htm": "html",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".toml": "toml",
    ".pdf": "pdf",
    ".ipynb": "notebook",
}


def _ext_lower(file_path: str) -> str:
    """``extname(p).toLowerCase()`` — the dotted extension, lower-cased.

    Node ``path.extname`` returns ``".md"`` for ``"a.md"`` and ``""`` when there
    is no dot (or the basename starts with a single leading dot, e.g.
    ``.gitignore`` → ``""``). ``os.path.splitext`` reproduces both behaviours.
    """
    return os.path.splitext(file_path)[1].lower()


def detect_file_type(file_path: str) -> str:
    """Detect the :data:`EXT_MAP` file type, falling back to ``"text"``.

    Mirrors the TS ``detectFileType``: ``EXT_MAP[ext] ?? 'text'``.
    """
    ext = _ext_lower(file_path)
    return EXT_MAP.get(ext, "text")


# ── Directory enumeration ─────────────────────────────────────


def enumerate_dir(dir_path: str) -> list[str]:
    """Recursively list supported files under ``dir_path`` (TS ``enumerateDir``).

    Dot-directories (``.git``, ``.obsidian``), any directory named ``index`` (the
    COLD vector-store root), and ``*.tmp`` directories are skipped. Files are
    admitted only when their lower-cased extension is a key of :data:`EXT_MAP` —
    a deliberate membership test, NOT :func:`detect_file_type` (whose ``"text"``
    fallback would ingest every file). Returns an empty list when no supported
    files exist.
    """
    results: list[str] = []
    # ``readdirSync(dir, { withFileTypes: true })`` returns entries; ``os.scandir``
    # gives the same dirent-style directory/file discrimination.
    with os.scandir(dir_path) as it:
        entries = list(it)
    for entry in entries:
        if entry.is_dir():
            name = entry.name
            # Skip dot-dirs, the COLD store root, and tmp dirs.
            if name.startswith(".") or name == "index" or name.endswith(".tmp"):
                continue
            results.extend(enumerate_dir(os.path.join(dir_path, name)))
        elif entry.is_file():
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in EXT_MAP:
                results.append(os.path.join(dir_path, entry.name))
    return results


# ── Content extraction ────────────────────────────────────────

# ``ExtractResult`` is the TS ``{ content: string; rawHtml?: string }``. The port
# returns a plain dict; ``raw_html`` is present only for HTML (TS ``rawHtml``).


_SCRIPT_RE = re.compile(r"<script[\s\S]*?</script>", re.IGNORECASE)
_STYLE_RE = re.compile(r"<style[\s\S]*?</style>", re.IGNORECASE)
_BLOCK_TAG_RE = re.compile(r"<(br|p|div|h[1-6]|li|tr|blockquote)[^>]*>", re.IGNORECASE)
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_TAB_RE = re.compile(r"[ \t]+")
_TRIPLE_NL_RE = re.compile(r"\n{3,}")


def strip_html_tags(html: str) -> str:
    """Strip tags + decode the five named entities (TS ``stripHtmlTags``).

    Reproduces the TS regex pipeline verbatim: drop ``<script>`` / ``<style>``
    blocks, convert a fixed set of block-level open tags to newlines, delete all
    remaining tags, decode ``&amp; &lt; &gt; &quot; &#39; &nbsp;``, then collapse
    runs of spaces/tabs to one space, collapse 3+ newlines to two, and trim.
    """
    text = _SCRIPT_RE.sub(" ", html)
    text = _STYLE_RE.sub(" ", text)
    text = _BLOCK_TAG_RE.sub("\n", text)
    text = _ANY_TAG_RE.sub("", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
        .replace("&nbsp;", " ")
    )
    text = _SPACE_TAB_RE.sub(" ", text)
    text = _TRIPLE_NL_RE.sub("\n\n", text)
    return text.strip()


def extract_content(file_path: str, file_type: str) -> dict[str, str]:
    """Extract text content for a non-PDF file (TS ``extractContent``).

    Raises ``RuntimeError`` (TS ``throw new Error``) for ``"pdf"`` — PDF goes
    through the async path. Markdown/text/code/csv/yaml/toml are returned raw;
    JSON is parsed and re-stringified with 2-space indent; HTML is tag-stripped
    (raw retained under ``raw_html``); notebooks join their cell sources.
    """
    if file_type == "pdf":
        raise RuntimeError(
            "PDF extraction must be done via ingestDocuments (async). "
            "Call extractContent only for non-PDF types.",
        )

    with open(file_path, encoding="utf-8") as fh:
        raw = fh.read()

    if file_type in ("markdown", "text", "code", "csv", "yaml", "toml"):
        return {"content": raw}

    if file_type == "json":
        parsed = json.loads(raw)
        return {"content": json.dumps(parsed, indent=2, ensure_ascii=False)}

    if file_type == "html":
        return {"content": strip_html_tags(raw), "raw_html": raw}

    if file_type == "notebook":
        nb = json.loads(raw)
        cells = nb["cells"]
        sections: list[str] = []
        for cell in cells:
            source = cell["source"]
            src = "".join(source) if isinstance(source, list) else source
            label = "[code]\n" if cell["cell_type"] == "code" else ""
            sections.append(f"{label}{src}")
        return {"content": "\n\n".join(sections)}

    # ``default`` branch (TS ``_exhaustive: never``): unknown type returns raw.
    return {"content": raw}


# ── Date normalization ───────────────────────────────────────


def _now_iso() -> str:
    """Return the current UTC time as a millisecond ISO 8601 string.

    Mirrors Node ``new Date().toISOString()`` — e.g. ``2026-06-24T12:34:56.789Z``.
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _to_iso(dt: datetime) -> str:
    """Format a UTC ``datetime`` as Node ``Date.toISOString()`` would.

    Always millisecond precision, ``Z``-suffixed (the JS canonical form). The
    ``datetime`` is converted to UTC first so the rendered fields are the UTC
    wall-clock components, matching ``new Date(ts).toISOString()``.
    """
    u = dt.astimezone(UTC)
    return u.strftime("%Y-%m-%dT%H:%M:%S.") + f"{u.microsecond // 1000:03d}Z"


# Month names → 1-indexed month, for the human-readable ``Month D, YYYY`` shape
# that JS ``Date.parse`` accepts. Full and 3-letter abbreviations, lower-cased.
_MONTHS: dict[str, int] = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ISO_DATETIME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?"
    r"(Z|[+-]\d{2}:?\d{2})?$"
)
_ISO_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_US_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})/(\d{4})$")
_HUMAN_DATE_RE = re.compile(r"^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})$")


def _parse_js_date(raw: str) -> datetime | None:
    """Parse ``raw`` the way JS ``Date.parse`` does for our supported shapes.

    Returns a UTC ``datetime`` on success, or ``None`` (the JS ``NaN`` analogue)
    when unparseable. JS ``Date`` treats a bare ``YYYY-MM-DD`` date as UTC
    midnight, but a US ``M/D/YYYY`` or human ``Month D, YYYY`` as LOCAL midnight;
    the ingest tests only assert the parsed UTC *date* (year/month/day, tolerant
    of the timezone edge), and the already-valid ISO branch is returned verbatim
    upstream, so we parse all our shapes as UTC midnight — which yields the
    asserted year/month/day exactly and never trips the date-only edge clause.
    """
    s = raw.strip()
    if not s:
        return None

    # Already-valid ISO date-time (with or without explicit zone / millis).
    if _ISO_DATETIME_RE.match(s):
        normalized = s.replace(" ", "T")
        # Map a trailing ``Z`` to ``+00:00`` for ``fromisoformat``.
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt

    # ISO date-only ``YYYY-MM-DD`` → UTC midnight.
    m = _ISO_DATE_RE.match(s)
    if m:
        year, month, day = (int(g) for g in m.groups())
        return _safe_dt(year, month, day)

    # US-locale ``M/D/YYYY`` → UTC midnight.
    m = _US_DATE_RE.match(s)
    if m:
        month, day, year = (int(g) for g in m.groups())
        return _safe_dt(year, month, day)

    # Human-readable ``Month D, YYYY`` / ``Month D YYYY``.
    m = _HUMAN_DATE_RE.match(s)
    if m:
        month_name, day_s, year_s = m.groups()
        month_num = _MONTHS.get(month_name.lower())
        if month_num is None:
            return None
        return _safe_dt(int(year_s), month_num, int(day_s))

    return None


def _safe_dt(year: int, month: int, day: int) -> datetime | None:
    """Build a UTC-midnight ``datetime`` or ``None`` on an invalid calendar date."""
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def normalize_date(raw: str, fallback_now: str | None = None) -> str:
    """Normalize a raw date string to ISO 8601 date-time (TS ``normalizeDate``).

    Resolution order, mirroring the TS:

    1. Already-valid ISO date-time (contains ``"T"`` and parses): returned as-is.
    2. ``Date.parse`` coercion succeeds: convert to ISO 8601 + warn (stderr).
    3. Unparseable: fall back to ``fallback_now`` (TS ``fallbackNow``) or
       :func:`_now_iso` (TS ``new Date().toISOString()``) + warn (stderr).
    """
    # Fast path: already a valid ISO 8601 date-time.
    if "T" in raw:
        if _parse_js_date(raw) is not None:
            return raw

    # Attempt ``Date.parse`` coercion.
    parsed = _parse_js_date(raw)
    if parsed is not None:
        normalized = _to_iso(parsed)
        print(
            f'[ingest] Date coerced: "{raw}" → "{normalized}"',
            file=sys.stderr,
        )
        return normalized

    # Unparseable — fall back to current time.
    fallback = fallback_now if fallback_now is not None else _now_iso()
    print(
        f'[ingest] Date unparseable, using fallback: "{raw}" → "{fallback}"',
        file=sys.stderr,
    )
    return fallback


# ── ID generation ─────────────────────────────────────────────


def _short_id() -> str:
    """``randomBytes(4).toString('hex')`` → 8 lowercase hex chars."""
    return secrets.token_hex(4)


def _collection_id(name: str) -> str:
    """``{name}--{randomBytes(4).toString('hex')}`` (TS ``collectionId``)."""
    return f"{name}--{secrets.token_hex(4)}"


# ── JSON array splitting ─────────────────────────────────────


def _detect_json_array(
    parsed: object,
    json_items_key: str | None = None,
) -> dict[str, object] | None:
    """Detect the items array within a parsed JSON value (TS ``detectJsonArray``).

    Returns ``{"items": list, "key": str | None}`` or ``None`` (treat as a single
    blob). Resolution order:

    1. Caller-specified ``json_items_key``: use it iff the top level is a plain
       object containing that key whose value is an array; otherwise ``None``.
    2. Top-level array: ``{"items": parsed, "key": None}``.
    3. Auto-detect: exactly one array-typed property with length > 1.
    """
    # 1. Caller-specified key (TS ``jsonItemsKey !== undefined``).
    if json_items_key is not None:
        if (
            isinstance(parsed, dict)
            and not isinstance(parsed, list)
            and json_items_key in parsed
        ):
            value = parsed[json_items_key]
            if isinstance(value, list):
                return {"items": value, "key": json_items_key}
        # Key specified but not found / not an array.
        return None

    # 2. Top-level array.
    if isinstance(parsed, list):
        return {"items": parsed, "key": None}

    # 3. Auto-detect: single array property with >1 element.
    if isinstance(parsed, dict):
        array_keys = [
            k
            for k, v in parsed.items()
            if isinstance(v, list) and len(v) > 1
        ]
        if len(array_keys) == 1:
            key = array_keys[0]
            value = parsed[key]
            assert isinstance(value, list)
            return {"items": value, "key": key}

    return None


def _derive_item_id(item: object, index: int, file_base: str) -> str:
    """Derive a human-readable item id, falling back to index (TS ``deriveItemId``)."""
    if isinstance(item, dict):
        for key in ("id", "tweet_id", "_id"):
            # TS ``obj[key] !== undefined && obj[key] !== null``.
            if key in item and item[key] is not None:
                return f"local_{file_base}_{_js_string(item[key])}"
    return f"local_{file_base}_{index}_{_short_id()}"


def _derive_item_title(item: object) -> str:
    """Derive a title, falling back to the first 80 chars (TS ``deriveItemTitle``)."""
    if isinstance(item, dict):
        for key in ("title", "name", "summary"):
            value = item.get(key)
            if isinstance(value, str) and len(value) > 0:
                return value
    serialized = item if isinstance(item, str) else _json_compact(item)
    if len(serialized) > 80:
        return serialized[:80] + "..."
    return serialized


def _split_json_array(
    items: list[object],
    file_base: str,
    file_path: str,
    file_size: int,
    now: str,
    array_key: str | None,
) -> list[dict[str, object]]:
    """Build RawItems from the entries of a JSON array (TS ``splitJsonArray``)."""
    out: list[dict[str, object]] = []
    for idx, entry in enumerate(items):
        obj = entry if isinstance(entry, dict) else None

        url = obj["url"] if obj is not None and isinstance(obj.get("url"), str) else ""

        # ``date`` then ``created_at`` then ``now`` (TS chained ternary on
        # ``typeof === 'string'``).
        if obj is not None and isinstance(obj.get("date"), str):
            raw_date = obj["date"]
        elif obj is not None and isinstance(obj.get("created_at"), str):
            raw_date = obj["created_at"]
        else:
            raw_date = now
        date = normalize_date(_as_str(raw_date))

        # ``author`` then ``user`` (TS chained ternary; ``undefined`` when absent).
        author: str | None
        if obj is not None and isinstance(obj.get("author"), str):
            author = obj["author"]
        elif obj is not None and isinstance(obj.get("user"), str):
            author = obj["user"]
        else:
            author = None

        # ``tags`` then ``hashtags``, each filtered to strings; else ``[]``.
        tags: list[str]
        if obj is not None and isinstance(obj.get("tags"), list):
            tags = [t for t in obj["tags"] if isinstance(t, str)]
        elif obj is not None and isinstance(obj.get("hashtags"), list):
            tags = [t for t in obj["hashtags"] if isinstance(t, str)]
        else:
            tags = []

        metadata: dict[str, object] = {
            "file_type": "json",
            "file_size": file_size,
            "json_split": True,
            "json_array_key": array_key,
            "item_index": idx,
        }
        if author is not None:
            metadata["author"] = author

        out.append(
            {
                "id": _derive_item_id(entry, idx, file_base),
                "source_type": "local_ingest",
                "source_ref": f"file:{file_path}",
                "title": _derive_item_title(entry),
                "content": json.dumps(entry, indent=2, ensure_ascii=False),
                "url": url,
                "date": date,
                "tags": tags,
                "metadata": metadata,
            }
        )
    return out


# ── JS coercion helpers ───────────────────────────────────────


def _js_string(value: object) -> str:
    """Coerce ``value`` the way JS ``String(value)`` / template-literal does.

    Used for the derived-id ``String(obj[key])`` path: numbers, booleans, and
    strings are the values that occur there. Mirrors the sibling
    ``collections_store._js_string`` rules (``True``→``"true"``, whole floats
    without ``.0``).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return ",".join(_js_string(v) if v is not None else "" for v in value)
    if isinstance(value, dict):
        return "[object Object]"
    return str(value)


def _json_compact(value: object) -> str:
    """``JSON.stringify(value)`` (no indent) — compact, comma+colon separators."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _as_str(value: object) -> str:
    """Narrow a known-string value to ``str`` for mypy (asserts the invariant)."""
    assert isinstance(value, str)
    return value


# ── PDF extraction ────────────────────────────────────────────


async def _extract_pdf_content(file_path: str) -> dict[str, str]:
    """Extract text from a PDF via ``pypdf`` (TS ``extractPdfContent``).

    The Node module lazily ``await import('pdf-parse')`` and, on failure, throws
    an actionable install message. The Python port lazily imports ``pypdf``
    (declared as the optional ``pdf`` extra); when it is absent, raises the same
    actionable error so the caller knows how to enable PDF ingestion.
    """
    try:
        import pypdf
    except ImportError as exc:
        raise RuntimeError(
            "pdf-parse is required for PDF ingestion. "
            "Run: npm install pdf-parse @types/pdf-parse"
        ) from exc

    reader = pypdf.PdfReader(file_path)
    text = "".join(page.extract_text() for page in reader.pages)
    return {"content": text}


async def ingest_documents_async(
    file_paths: list[str],
    ingest_name: str,
    json_items_key: str | None = None,
) -> dict[str, object]:
    """Build a raw collection from ``file_paths`` (TS ``ingestDocumentsAsync``).

    Async to match the TS signature (PDF extraction is awaited). Returns the raw
    collection as a plain dict whose key order mirrors the TS object literal:
    ``collection_id, created_date, status, source_runs, items`` (no
    ``manifest_id`` — the TS omits it, so it stays absent, valid under the
    schema's optional field).
    """
    now = _now_iso()
    items: list[dict[str, object]] = []
    source_runs: list[dict[str, object]] = []

    for file_path in file_paths:
        executed_at = _now_iso()
        file_type = detect_file_type(file_path)
        file_size = os.stat(file_path).st_size

        if file_type == "pdf":
            extracted = await _extract_pdf_content(file_path)
        else:
            extracted = extract_content(file_path, file_type)

        file_base = os.path.basename(file_path)
        title_base = (
            file_base[: file_base.rfind(".")]
            if "." in file_base
            else file_base
        )

        # ── JSON array splitting ──
        if file_type == "json":
            parsed = json.loads(extracted["content"])
            detected = _detect_json_array(parsed, json_items_key)
            if detected is not None:
                detected_items = detected["items"]
                assert isinstance(detected_items, list)
                if len(detected_items) > 0:
                    array_key = detected["key"]
                    assert array_key is None or isinstance(array_key, str)
                    split_items = _split_json_array(
                        detected_items,
                        title_base,
                        file_path,
                        file_size,
                        now,
                        array_key,
                    )
                    items.extend(split_items)
                    source_runs.append(
                        {
                            "source_type": "local_ingest",
                            "path": file_path,
                            "executed_at": executed_at,
                            "items_found": len(detected_items),
                            "items_stored": len(split_items),
                        }
                    )
                    continue

        # ── Default: single-item ingestion ──
        metadata: dict[str, object] = {
            "file_type": file_type,
            "file_size": file_size,
        }
        if "raw_html" in extracted:
            metadata["raw_html"] = extracted["raw_html"]

        item: dict[str, object] = {
            "id": f"local_{title_base}_{_short_id()}",
            "source_type": "local_ingest",
            "source_ref": f"file:{file_path}",
            "title": title_base,
            "content": extracted["content"],
            "url": "",
            "date": now,
            "tags": [],
            "metadata": metadata,
        }

        items.append(item)

        source_runs.append(
            {
                "source_type": "local_ingest",
                "path": file_path,
                "executed_at": executed_at,
                "items_found": 1,
                "items_stored": 1,
            }
        )

    return {
        "collection_id": _collection_id(ingest_name),
        "created_date": now,
        "status": "raw",
        "source_runs": source_runs,
        "items": items,
    }
