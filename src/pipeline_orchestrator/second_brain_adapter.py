"""Live vector adapter for the OS-owned ``second_brain`` COLD store (port of
``legacy-node/second-brain-adapter.ts``; binding spec docs/10 §9.2).

Shells out (read-only) to the ``second_brain`` Python CLI to surface semantic
recall. Hard invariants (SP3 program design):

* **#4 COLD never blocks** — any failure / mismatch / unavailable degrades to
  ``provider_not_configured``; :meth:`SecondBrainAdapter.vector_search` NEVER
  raises out.
* **#5 ONE store, verifiable** — before any query the adapter calls
  ``status --json`` and asserts byte-equality of ``index_path`` (and
  ``embedder_version`` when ``SECOND_BRAIN_EMBEDDER_VERSION`` is set) against the
  configured store. The adapter issues ONLY read-only ``status`` / ``query`` —
  NEVER ``build`` / ``rebuild``, and never creates any directory or collection.

Parity notes:

* **Env read directly, not via ``config.py``.** ``vector_search`` reads
  ``os.environ.get("SECOND_BRAIN_INDEX_PATH")`` /
  ``os.environ.get("SECOND_BRAIN_EMBEDDER_VERSION")`` on every call (mirroring TS
  ``process.env.*``). The ``@functools.cache``'d readers in ``config.py`` would
  return stale values when tests toggle the env per-test, so they are not used.
* **Concrete return type.** :func:`create_second_brain_adapter` is annotated to
  return the concrete :class:`SecondBrainAdapter` (which implements only
  ``vector_search``), not the ``KBProviderAdapter`` Protocol (whose
  ``sql_query`` would be required under mypy --strict). Wiring it into the
  registry is a later task's concern.
* **Non-throwing default runner.** :func:`default_run_cli` never raises out:
  ``subprocess`` failures (timeout / missing venv / OS error) are mapped to a
  non-zero ``code`` so ``vector_search`` can branch on it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .kb_client import KBClient, VectorSearchParams, VectorSearchResult

# ── Frozen SP1 CLI contract (result of a single read-only CLI invocation) ──


@dataclass(frozen=True)
class RunCliResult:
    """Result of a single read-only CLI invocation (TS ``RunCliResult``)."""

    code: int
    stdout: str
    stderr: str


#: Spawns ``<PY> -m second_brain.cli <args>`` and captures its output.
#: Dependency-injectable so tests can pass a fake (no real venv needed).
RunCli = Callable[[list[str], int], Awaitable[RunCliResult]]

# Subprocess env for stdout hygiene — the CLI emits exactly ONE JSON line on
# stdout; these silence library chatter that would otherwise contaminate it.
HYGIENE_ENV: dict[str, str] = {
    "HF_HUB_DISABLE_PROGRESS_BARS": "1",
    "TRANSFORMERS_VERBOSITY": "error",
    "TOKENIZERS_PARALLELISM": "false",
}

STATUS_TIMEOUT_MS = 20_000
QUERY_TIMEOUT_MS = 60_000


async def default_run_cli(args: list[str], timeout_ms: int) -> RunCliResult:
    """Default ``run_cli``: run the second_brain venv python with the
    ``-m second_brain.cli`` module entrypoint, non-throwing on non-zero exit so
    the adapter can branch on ``code``.

    Never raises out — a timeout / missing venv / OS error maps to a non-zero
    ``code`` with whatever stdout/stderr was captured (empty on spawn failure).
    """
    py = os.environ.get("SECOND_BRAIN_PYTHON", "/opt/second-brain/bin/python")
    try:
        proc = subprocess.run(
            [py, "-m", "second_brain.cli", *args],
            env={**os.environ, **HYGIENE_ENV},
            capture_output=True,
            text=True,
            timeout=timeout_ms / 1000,
            check=False,
        )
    except subprocess.TimeoutExpired as err:
        return RunCliResult(
            code=1,
            stdout=err.stdout or "" if isinstance(err.stdout, str) else "",
            stderr=err.stderr or "" if isinstance(err.stderr, str) else "",
        )
    except (FileNotFoundError, OSError):
        return RunCliResult(code=1, stdout="", stderr="")
    return RunCliResult(code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


# ── Internal helpers ──────────────────────────────────────────

_LINE_SPLIT = re.compile(r"\r?\n")


def parse_json_line(stdout: str) -> dict[str, object] | None:
    """Parse the single JSON line the CLI emits on stdout; ``None`` on failure.

    The CLI emits exactly one JSON line; if logs leaked, take the last
    non-empty line that parses as an object (walk bottom-up).
    """
    trimmed = stdout.strip()
    if not trimmed:
        return None
    lines = [line for line in _LINE_SPLIT.split(trimmed) if line.strip()]
    for line in reversed(lines):
        try:
            parsed = json.loads(line)
        except (ValueError, json.JSONDecodeError):
            # try the next line up
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _js_token(status: dict[str, object], key: str) -> str:
    """Render a status value the way a JS template literal would: an absent
    key as ``undefined``, a JSON ``null`` as ``null``, else its string form
    (mirrors the TS ``${status.<key>}`` interpolation in the mismatch strings)."""
    if key not in status:
        return "undefined"
    value = status[key]
    if value is None:
        return "null"
    return str(value)


def _not_configured(error: str) -> VectorSearchResult:
    return {
        "status": "provider_not_configured",
        "results": [],
        "results_count": 0,
        "error": error,
    }


# ── Adapter ───────────────────────────────────────────────────


class SecondBrainAdapter:
    """The ``vector:second_brain`` provider adapter.

    Implements only ``vector_search`` (the TS object literal implements only
    ``vectorSearch``); it deliberately has no ``sql_query`` method.
    """

    type = "vector"
    provider = "second_brain"

    def __init__(self, run_cli: RunCli) -> None:
        self._run_cli = run_cli

    async def vector_search(
        self, client: KBClient, params: VectorSearchParams
    ) -> VectorSearchResult:
        try:
            # (a) Env gate — no store configured → no spawn at all.
            index_path = os.environ.get("SECOND_BRAIN_INDEX_PATH")
            if not index_path:
                return _not_configured("SECOND_BRAIN_INDEX_PATH unset")

            # (b) Read-only status probe.
            status_run = await self._run_cli(["status", "--json"], STATUS_TIMEOUT_MS)
            if status_run.code != 0:
                return _not_configured(f"second_brain status exited {status_run.code}")
            status = parse_json_line(status_run.stdout)
            if status is None:
                return _not_configured("second_brain status: unparseable stdout")
            if status.get("enabled") is not True:
                return _not_configured("second_brain store not enabled")

            # (c) Handshake (invariant #5) — byte-equality before any query.
            if status.get("index_path") != index_path:
                return _not_configured(
                    'second_brain store mismatch: index_path '
                    f'"{_js_token(status, "index_path")}" '
                    f'!== SECOND_BRAIN_INDEX_PATH "{index_path}"'
                )
            expected_embedder = os.environ.get("SECOND_BRAIN_EMBEDDER_VERSION")
            actual_embedder = status.get("embedder_version")
            if expected_embedder and actual_embedder != expected_embedder:
                return _not_configured(
                    "second_brain store mismatch: "
                    f'embedder_version "{_js_token(status, "embedder_version")}" '
                    f'!== SECOND_BRAIN_EMBEDDER_VERSION "{expected_embedder}"'
                )

            # (d) Read-only query (positional text arg, not stdin).
            top_k = params["top_k"]
            query_run = await self._run_cli(
                ["query", params["query"], "--top-k", str(top_k), "--json"],
                QUERY_TIMEOUT_MS,
            )
            if query_run.code != 0:
                return _not_configured(f"second_brain query exited {query_run.code}")
            query_result = parse_json_line(query_run.stdout)
            if query_result is None:
                return _not_configured("second_brain query: unparseable stdout")

            query_status = query_result.get("status")
            if query_status == "ok":
                raw_results = query_result.get("results")
                results = raw_results if isinstance(raw_results, list) else []
                return {
                    "status": "ok",
                    "results": results,
                    "results_count": len(results),
                }
            if query_status == "empty":
                return {"status": "ok", "results": [], "results_count": 0}
            # 'unavailable' or anything unexpected → degrade.
            reason = query_result.get("reason")
            suffix = f": {reason}" if reason else ""
            return _not_configured(f"second_brain query unavailable{suffix}")
        except Exception as err:  # noqa: BLE001
            # (e) Invariant #4 — never throw out of the adapter.
            return _not_configured(f"second_brain adapter error: {err}")


def create_second_brain_adapter(
    run_cli: RunCli = default_run_cli,
) -> SecondBrainAdapter:
    """Build the ``vector:second_brain`` provider adapter.

    ``run_cli`` is an optional injected CLI runner (tests pass a fake; the
    default spawns the real venv). The adapter only ever asks ``run_cli`` for
    ``status`` and ``query`` — it has no code path that runs ``build`` /
    ``rebuild``.
    """
    return SecondBrainAdapter(run_cli)
