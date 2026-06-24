"""Pure-Python Okapi BM25 ranked text search (port of ``legacy-node/bm25.ts``).

Byte-faithful behavioral port of the Node ``bm25.ts`` module: tokenization,
index build, persistence, load, and ranked search over normalized
:class:`~pipeline_orchestrator.collections_store.ResearchItem` records.

Contract (spec §9.1):
  * Okapi BM25 with ``K1=1.5``, ``B=0.75``.
  * Tokenizer: lowercase, split on ``[^a-z0-9]+``, drop tokens shorter than 2.
  * Doc text = ``f"{title} {tags} {content}"`` (tags space-joined here, but
    comma-joined when stored as the ``tags`` string field).
  * ``idf = log((N - df + 0.5) / (df + 0.5) + 1)``.
  * Per query term: ``idf * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * len / avgdl))``.
  * Tag filter is tag-OR (lowercased, comma-split, trimmed; a doc passes if
    ANY of its tags is in the filter set).

Parity notes:
  * **Nullish-coalescing.** Every TS ``?? 0`` lookup becomes ``dict.get(k, 0)``
    (never ``or``), so an explicit stored ``0`` survives rather than falling
    through to a fallback.
  * **avgdl guard.** The denominator uses ``(self._avgdl or 1)`` to mirror the
    Node ``(this.avgdl || 1)`` guard against a zero ``avgdl``.
  * **Stable sort.** ``sorted(..., key=score, reverse=True)`` is stable and
    keeps insertion order for tied scores, matching V8's stable sort under the
    ``b.score - a.score`` comparator. The ``score > 0`` filter is applied
    BEFORE the sort, then ``[:top_k]``.
  * **Persisted key order** mirrors the Node object-literal / spread order:
    top-level ``collection_name, item_count, avg_doc_length, created_at,
    item_ids, docs, idf``; each doc entry ``item_id, title, tags, length,
    terms``.
  * **JSON serialization** mirrors Node ``JSON.stringify(data, null, 2)``
    (``json.dumps(..., indent=2, ensure_ascii=False)`` — ``ensure_ascii=False``
    keeps non-ASCII raw, matching Node's UTF-8 output).
  * **Path construction** is OS-native: :func:`bm25_index_dir` mirrors Node
    ``resolve(join(...))`` with ``os.path.abspath(os.path.join(...))``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from math import log
from typing import TypedDict, cast

from pipeline_orchestrator.collections_store import ResearchItem

# ── Constants ────────────────────────────────────────────────

K1 = 1.5
B = 0.75

_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


# ── Type shapes ──────────────────────────────────────────────


@dataclass(frozen=True)
class SearchHitMetadata:
    """Per-hit metadata (mirrors the inline TS ``{ title; tags }``)."""

    title: str
    tags: str


@dataclass(frozen=True)
class SearchHit:
    """A single ranked search result (mirrors the TS ``SearchHit``)."""

    item_id: str
    score: float
    metadata: SearchHitMetadata


@dataclass(frozen=True)
class BM25IndexMetadata:
    """Index metadata returned by :meth:`BM25Index.build` (TS ``BM25IndexMetadata``)."""

    collection_name: str
    item_count: int
    avg_doc_length: float
    created_at: str
    item_ids: list[str]


@dataclass
class DocEntry:
    """An indexed document (mirrors the internal TS ``DocEntry``)."""

    item_id: str
    title: str
    tags: str
    length: int
    terms: dict[str, int]


class _PersistedIndex(TypedDict):
    """Shape of the persisted ``index.json`` (TS ``PersistedIndex``)."""

    collection_name: str
    item_count: int
    avg_doc_length: float
    created_at: str
    item_ids: list[str]
    docs: list[dict[str, object]]
    idf: dict[str, float]


# ── Helpers ──────────────────────────────────────────────────


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Mirrors Node ``new Date().toISOString()`` — a millisecond-precision,
    ``Z``-suffixed UTC timestamp (e.g. ``2026-04-10T12:34:56.789Z``).
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def _tokenize(text: str) -> list[str]:
    """Lowercase, split on ``[^a-z0-9]+``, drop tokens shorter than 2.

    Mirrors the TS ``tokenize``: ``re.split`` on an empty/all-delimiter string
    yields ``['']`` (and leading/trailing empties), all dropped by the
    ``len >= 2`` filter.
    """
    return [t for t in _TOKEN_SPLIT.split(text.lower()) if len(t) >= 2]


def _term_frequencies(tokens: list[str]) -> dict[str, int]:
    """Count occurrences of each token (mirrors the TS ``termFrequencies``)."""
    tf: dict[str, int] = {}
    for t in tokens:
        tf[t] = tf.get(t, 0) + 1
    return tf


# ── BM25Index ────────────────────────────────────────────────


class BM25Index:
    """In-memory Okapi BM25 index with disk persistence (TS ``BM25Index``)."""

    def __init__(self) -> None:
        self._docs: list[DocEntry] = []
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0
        self._metadata: BM25IndexMetadata | None = None
        self._index_dir: str | None = None

    def build(
        self, items: list[ResearchItem], collection_name: str
    ) -> BM25IndexMetadata:
        """Build the index from ``items`` and return its metadata."""
        n = len(items)

        self._docs = []
        for item in items:
            text = f"{item.title or ''} {' '.join(item.tags)} {item.content}"
            tokens = _tokenize(text)
            self._docs.append(
                DocEntry(
                    item_id=item.id,
                    title=item.title or "",
                    tags=",".join(item.tags),
                    length=len(tokens),
                    terms=_term_frequencies(tokens),
                )
            )

        total_length = sum(d.length for d in self._docs)
        self._avgdl = total_length / n if n > 0 else 0.0

        df: dict[str, int] = {}
        for doc in self._docs:
            for term in doc.terms:
                df[term] = df.get(term, 0) + 1
        self._idf = {}
        for term, doc_freq in df.items():
            self._idf[term] = log((n - doc_freq + 0.5) / (doc_freq + 0.5) + 1)

        self._metadata = BM25IndexMetadata(
            collection_name=collection_name,
            item_count=n,
            avg_doc_length=self._avgdl,
            created_at=_now_iso(),
            item_ids=[item.id for item in items],
        )

        return self._metadata

    def persist(self, index_dir: str) -> None:
        """Write the built index to ``{index_dir}/index.json``."""
        if self._metadata is None:
            raise RuntimeError("Cannot persist: index not built")
        if not os.path.exists(index_dir):
            os.makedirs(index_dir, exist_ok=True)
        self._index_dir = index_dir

        data: _PersistedIndex = {
            "collection_name": self._metadata.collection_name,
            "item_count": self._metadata.item_count,
            "avg_doc_length": self._metadata.avg_doc_length,
            "created_at": self._metadata.created_at,
            "item_ids": self._metadata.item_ids,
            "docs": [
                {
                    "item_id": d.item_id,
                    "title": d.title,
                    "tags": d.tags,
                    "length": d.length,
                    "terms": d.terms,
                }
                for d in self._docs
            ],
            "idf": self._idf,
        }
        with open(os.path.join(index_dir, "index.json"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, ensure_ascii=False))

    def load_index(self, index_dir: str) -> None:
        """Load a persisted index from ``{index_dir}/index.json``."""
        self._index_dir = index_dir
        with open(os.path.join(index_dir, "index.json"), encoding="utf-8") as fh:
            data: _PersistedIndex = json.load(fh)
        self._docs = [
            DocEntry(
                item_id=str(d["item_id"]),
                title=str(d["title"]),
                tags=str(d["tags"]),
                length=int(cast(int, d["length"])),
                terms=dict(cast("dict[str, int]", d["terms"])),
            )
            for d in data["docs"]
        ]
        self._idf = data["idf"]
        self._avgdl = data["avg_doc_length"]
        self._metadata = BM25IndexMetadata(
            collection_name=data["collection_name"],
            item_count=data["item_count"],
            avg_doc_length=data["avg_doc_length"],
            created_at=data["created_at"],
            item_ids=data["item_ids"],
        )

    def is_built(self, index_dir: str | None = None) -> bool:
        """Report whether the index is built (in memory or on disk)."""
        directory = index_dir if index_dir is not None else self._index_dir
        if not directory:
            return self._metadata is not None
        return os.path.exists(os.path.join(directory, "index.json"))

    def get_metadata(self) -> BM25IndexMetadata | None:
        """Return the built index metadata, or ``None`` if not built."""
        return self._metadata

    def search(
        self,
        query: str,
        top_k: int = 10,
        filter_tags: list[str] | None = None,
    ) -> list[SearchHit]:
        """Return up to ``top_k`` ranked hits for ``query`` (TS ``search``).

        ``filter_tags`` mirrors the TS ``filter?: { tags?: string[] }``: when
        non-empty, only docs sharing at least one tag are scored.
        """
        query_terms = _tokenize(query)
        if len(query_terms) == 0:
            return []

        candidates = self._docs
        if filter_tags:
            filter_set = {t.lower() for t in filter_tags}
            filtered: list[DocEntry] = []
            for doc in candidates:
                doc_tags = [t.strip() for t in doc.tags.lower().split(",")]
                if any(t in filter_set for t in doc_tags):
                    filtered.append(doc)
            candidates = filtered

        scored: list[SearchHit] = []
        for doc in candidates:
            score = 0.0
            for term in query_terms:
                tf = doc.terms.get(term, 0)
                if tf == 0:
                    continue
                idf = self._idf.get(term, 0)
                numerator = tf * (K1 + 1)
                denominator = tf + K1 * (1 - B + B * doc.length / (self._avgdl or 1))
                score += idf * (numerator / denominator)
            scored.append(
                SearchHit(
                    item_id=doc.item_id,
                    score=score,
                    metadata=SearchHitMetadata(title=doc.title, tags=doc.tags),
                )
            )

        positive = [h for h in scored if h.score > 0]
        positive.sort(key=lambda h: h.score, reverse=True)
        return positive[:top_k]


# ── Index path resolver ──────────────────────────────────────


def bm25_index_dir(base_dir: str, collection_name: str) -> str:
    """Resolve the on-disk index directory for ``collection_name``.

    Mirrors Node ``resolve(join(baseDir, 'kb', 'bm25', collectionName))``.
    """
    return os.path.abspath(os.path.join(base_dir, "kb", "bm25", collection_name))
