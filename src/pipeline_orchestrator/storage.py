"""Artifact storage + filesystem-safe run-name sanitizers (port of ``storage.ts``).

Byte-exact behavioral port of the Node ``storage.ts`` module (spec §7.3 / §7.4).
Covers two surfaces:

* **Sanitizers + per-run path helpers (Feature C):** ``sanitize_run_name``,
  ``sanitize_run_timestamp``, ``get_run_data_dir``, ``get_artifact_dir``,
  ``storage_key_to_subtype`` — they turn a human run name + ISO timestamp into a
  cross-platform-safe per-run directory tree rooted at
  ``{base_dir}/runs/{name}-{timestamp}/``.
* **Legacy flat storage:** ``store_artifact`` / ``load_artifact`` /
  ``list_artifacts`` / ``get_artifact_path`` / ``file_exists`` resolve a
  ``storage_key`` against ``StorageConfig.paths`` and read/write a flat
  per-type directory. ``persist_artifact`` is the run-scoped writer used when a
  run's ``run_data_dir`` is populated.

Path construction is OS-native (``os.path.join``), matching Node ``path.join``;
on Windows that means backslash separators. JSON serialization mirrors Node:
``store``/``persist`` use ``JSON.stringify(artifact, null, 2)`` (Python
``json.dumps(artifact, indent=2)``); ``build_artifact_summary`` measures the
compact ``JSON.stringify(artifact)`` byte length (Python ``separators=(",", ":")``).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Literal

# ── Storage configuration ───────────────────────────────────────────
#
# Mirrors the TS ``StorageConfig`` interface (``types.ts``):
#   { base_dir: string; paths: Record<string, string> }


@dataclass
class StorageConfig:
    """Storage configuration: a base directory plus per-key subpath map."""

    base_dir: str
    paths: dict[str, str] = field(default_factory=dict)


def _resolve_dir(config: StorageConfig, storage_key: str) -> str:
    """Resolve a ``storage_key`` to ``{base_dir}/{paths[storage_key]}``.

    Raises ``ValueError`` (the Node code throws a plain ``Error``) when the key
    is absent or its subpath is falsy. The message lists the available keys in
    insertion order, matching the Node ``Object.keys(...).join(', ')``.
    """
    sub_path = config.paths.get(storage_key)
    if not sub_path:
        available = ", ".join(config.paths.keys())
        raise ValueError(
            f'Unknown storage key "{storage_key}". Available: {available}'
        )
    return os.path.join(config.base_dir, sub_path)


# ── Per-Run Hierarchical Artifact Directory Helpers (Feature C) ──────
#
# Replaces top-level per-type folders with per-run trees rooted at
# ``{base_dir}/runs/{sanitized_run_name}-{sanitized_timestamp}/``.
# Sanitization is deliberately conservative for cross-platform safety.

# Subtype path segments for per-run artifact directories.
ArtifactSubtype = Literal[
    "collections/curated",
    "collections/raw",
    "debates",
    "overviews",
    "specs",
    "scaffold",
]

# Maximum length (in characters) of a sanitized run name. Combined with the
# 21-character timestamp suffix this keeps the directory name well under common
# path-length limits even when nested under deep base dirs.
MAX_SANITIZED_RUN_NAME_LENGTH = 64

_NON_SLUG_CHARS = re.compile(r"[^a-zA-Z0-9-]")
_HYPHEN_RUNS = re.compile(r"-+")
_LEADING_HYPHENS = re.compile(r"^-+")
_TRAILING_HYPHENS = re.compile(r"-+$")
_UNSAFE_TIMESTAMP_CHARS = re.compile(r"[:.]")


def sanitize_run_name(name: str) -> str:
    """Sanitize a human-readable run name into a filesystem-safe slug.

    Rules (in source order):
      - Replace any character not in ``[a-zA-Z0-9-]`` with ``-``
      - Lowercase the result
      - Collapse runs of ``-`` and trim leading/trailing ``-``
      - Truncate to 64 characters then re-trim a trailing ``-`` from the cut

    Returns ``'unnamed-run'`` for inputs that sanitize to empty (all-whitespace,
    all-punctuation, or non-string / empty input).
    """
    if not isinstance(name, str) or len(name) == 0:
        return "unnamed-run"
    replaced = _NON_SLUG_CHARS.sub("-", name).lower()
    # Collapse runs of '-' so e.g. "fix:type/mismatch" -> "fix--type-mismatch"
    # -> "fix-type-mismatch".
    collapsed = _HYPHEN_RUNS.sub("-", replaced)
    # Trim leading and trailing hyphens.
    trimmed = _TRAILING_HYPHENS.sub("", _LEADING_HYPHENS.sub("", collapsed))
    if len(trimmed) == 0:
        return "unnamed-run"
    # Truncate then re-trim a trailing hyphen in case the cut landed inside a
    # hyphen sequence.
    truncated = _TRAILING_HYPHENS.sub("", trimmed[:MAX_SANITIZED_RUN_NAME_LENGTH])
    return truncated if len(truncated) > 0 else "unnamed-run"


def sanitize_run_timestamp(iso: str) -> str:
    """Sanitize an ISO 8601 timestamp into a filesystem-safe suffix.

    Replaces ``:`` and ``.`` with ``-`` (Windows disallows both in filenames).
    Other characters pass through. Non-string / empty inputs return an empty
    string so callers can detect a missing timestamp.

    Example: ``"2026-04-10T09:13:58.123Z"`` -> ``"2026-04-10T09-13-58-123Z"``.
    """
    if not isinstance(iso, str) or len(iso) == 0:
        return ""
    return _UNSAFE_TIMESTAMP_CHARS.sub("-", iso)


def get_run_data_dir(base_dir: str, run_name: str, timestamp: str) -> str:
    """Compute the per-run data directory path.

    ``{base_dir}/runs/{sanitized_run_name}-{sanitized_timestamp}``; if the
    timestamp is missing or sanitizes to empty, the suffix is omitted so the
    directory is ``{base_dir}/runs/{sanitized_run_name}``.
    """
    safe_name = sanitize_run_name(run_name)
    safe_timestamp = sanitize_run_timestamp(timestamp)
    dir_name = f"{safe_name}-{safe_timestamp}" if len(safe_timestamp) > 0 else safe_name
    return os.path.join(base_dir, "runs", dir_name)


def get_artifact_dir(run_data_dir: str, subtype: ArtifactSubtype) -> str:
    """Compute the per-run subdirectory for a given artifact subtype.

    Mirrors Node ``join(runDataDir, subtype)``. Because a subtype such as
    ``'collections/curated'`` contains a forward slash, ``os.path.join`` splits
    it into native segments — on Windows the result uses backslashes throughout,
    the same normalization Node's ``path.join`` performs.
    """
    return os.path.join(run_data_dir, *subtype.split("/"))


def storage_key_to_subtype(storage_key: str) -> ArtifactSubtype | None:
    """Map a ``storage_key`` to its ``ArtifactSubtype``, or ``None``.

    Returns ``None`` for keys with no run-scoped subtype mapping (e.g.
    ``'manifests'``, ``'codebase'``, ``'runs'``, or any unknown key) so callers
    can fall back to legacy path resolution without throwing.
    """
    mapping: dict[str, ArtifactSubtype] = {
        "raw_collections": "collections/raw",
        "curated_collections": "collections/curated",
        "overviews": "overviews",
        "specs": "specs",
        "debates": "debates",
        "scaffold": "scaffold",
    }
    return mapping.get(storage_key)


def persist_artifact(
    run_data_dir: str,
    subtype: ArtifactSubtype,
    file_name: str,
    artifact: object,
    force: bool | None = None,
) -> str:
    """Per-run artifact write helper (Feature C).

    Resolves the target directory via ``get_artifact_dir(run_data_dir, subtype)``
    and writes the artifact there, creating intermediate directories. Preserves
    the throw-on-collision-unless-force semantics of :func:`store_artifact` — the
    error message is byte-identical.

    Does NOT fall back to legacy paths; callers needing legacy support branch on
    ``run_data_dir`` themselves and call :func:`store_artifact`.

    Returns the path written. Raises ``FileExistsError`` if the file already
    exists and ``force`` is not ``True``.
    """
    directory = get_artifact_dir(run_data_dir, subtype)
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, file_name)
    if os.path.exists(file_path) and force is not True:
        raise FileExistsError(
            f'Artifact already exists at "{file_path}". Pass force=true to overwrite.'
        )
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(artifact, indent=2))
    return file_path


def store_artifact(
    config: StorageConfig,
    storage_key: str,
    file_name: str,
    artifact: object,
    force: bool | None = None,
) -> str:
    """Write an artifact to the legacy flat per-type directory.

    Resolves ``storage_key`` against ``config.paths``, creates the directory,
    and writes ``file_name`` there. Raises ``FileExistsError`` (matching the
    Node error message) if the file exists and ``force`` is not ``True``.
    """
    directory = _resolve_dir(config, storage_key)
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, file_name)
    if os.path.exists(file_path) and force is not True:
        raise FileExistsError(
            f'Artifact already exists at "{file_path}". Pass force=true to overwrite.'
        )
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(artifact, indent=2))
    return file_path


def load_artifact(
    config: StorageConfig,
    storage_key: str,
    file_name: str,
) -> object | None:
    """Load and JSON-parse an artifact from the legacy flat directory.

    Returns ``None`` when the file does not exist.
    """
    directory = _resolve_dir(config, storage_key)
    file_path = os.path.join(directory, file_name)
    if not os.path.exists(file_path):
        return None
    with open(file_path, encoding="utf-8") as fh:
        loaded: object = json.load(fh)
    return loaded


def list_artifacts(
    config: StorageConfig,
    storage_key: str,
) -> list[str]:
    """List the ``*.json`` files in the legacy flat directory for a key.

    Returns ``[]`` when the directory does not exist.
    """
    directory = _resolve_dir(config, storage_key)
    if not os.path.exists(directory):
        return []
    return [f for f in os.listdir(directory) if f.endswith(".json")]


def get_artifact_path(
    config: StorageConfig,
    storage_key: str,
    file_name: str,
) -> str:
    """Resolve the legacy flat path for ``file_name`` under ``storage_key``."""
    return os.path.join(_resolve_dir(config, storage_key), file_name)


def file_exists(
    config: StorageConfig,
    storage_key: str,
    file_name: str,
) -> bool:
    """Report whether ``file_name`` exists in the legacy flat directory."""
    directory = _resolve_dir(config, storage_key)
    return os.path.exists(os.path.join(directory, file_name))


@dataclass
class ArtifactSummary:
    """Lightweight summary of a stored artifact (mirrors the TS interface)."""

    artifact_type: str
    id: str
    key_count: int
    total_size_bytes: int
    truncated: bool
    preview_keys: list[str]


PREVIEW_KEY_LIMIT = 20


def build_artifact_summary(
    artifact: object,
    storage_key: str,
    file_name: str,
) -> ArtifactSummary:
    """Build an :class:`ArtifactSummary` for an artifact.

    ``key_count`` / ``preview_keys`` mirror JS ``Object.keys``: a ``dict``
    yields its keys, a ``list`` yields stringified indices (``['0', '1', ...]``),
    and any other (scalar / ``None``) yields no keys. ``total_size_bytes`` is the
    UTF-8 byte length of the compact JSON (``JSON.stringify(artifact)``), and
    ``truncated`` is set when the key count exceeds ``PREVIEW_KEY_LIMIT`` (20).
    """
    compact = json.dumps(artifact, separators=(",", ":"))
    if isinstance(artifact, dict):
        keys = [str(k) for k in artifact.keys()]
    elif isinstance(artifact, list):
        keys = [str(i) for i in range(len(artifact))]
    else:
        keys = []
    return ArtifactSummary(
        artifact_type=storage_key,
        id=file_name,
        key_count=len(keys),
        total_size_bytes=len(compact.encode("utf-8")),
        truncated=len(keys) > PREVIEW_KEY_LIMIT,
        preview_keys=keys[:PREVIEW_KEY_LIMIT],
    )
