from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import structlog
from mcp.server.fastmcp import FastMCP

from pipeline_orchestrator import config as _config_mod
from pipeline_orchestrator import run_state as _run_state_mod
from pipeline_orchestrator import storage as _storage_mod
from pipeline_orchestrator import toml_loader as _toml_loader_mod
from pipeline_orchestrator import validator as _validator_mod

if TYPE_CHECKING:
    from pipeline_orchestrator.models import (
        ArtifactRef,
        DebateState,
        PipelineConfig,
        QualityGate,
        RunState,
    )
    from pipeline_orchestrator.storage import StorageConfig
    from pipeline_orchestrator.validator import SchemaMap

# Frozen workflow blurb (R1 §0 / spec §3.2) — preserved verbatim from the Node
# server identity (server.ts `new Server({ name: 'pipeline' }, { instructions })`).
# Split across adjacent string literals (auto-concatenated) only to keep source
# lines under the 88-char ruff cap; the assembled value is byte-identical.
INSTRUCTIONS = (
    "Pipeline orchestrator MCP server. "
    "Reads pipeline.toml to understand phases and DAG edges. "
    "Initialize a run, resolve available phases, validate artifacts, "
    "and manage artifact storage. "
    "Includes embedded concept extraction (pipeline_cc_*), "
    "research synthesis (pipeline_synth_*), and feature request tools. "
    "Workflow: pipeline_init_run → pipeline_next_phases → "
    "pipeline_start_phase → (do work) → "
    "pipeline_validate_artifact → pipeline_store_artifact → "
    "pipeline_complete_phase → repeat."
)

# Frozen identity name (spec §2): the in-code `Server` identity is `pipeline`.
mcp: FastMCP = FastMCP("pipeline", instructions=INSTRUCTIONS)

_LOG_HANDLER_TAG = "pipeline_orchestrator_stderr"


def configure_logging() -> None:
    """Configure structlog so all logs go to sys.stderr only.

    stdout is the JSON-RPC framing channel under stdio transport; writing
    anything to stdout corrupts the protocol stream (spec §4.5/§15).
    Idempotent: a tagged StreamHandler(sys.stderr) is installed at most once.
    """
    root = logging.getLogger()
    # Only this module's own tagged handler is de-duplicated; any pre-existing
    # foreign StreamHandler(sys.stderr) is intentionally left in place
    # (stderr is protocol-safe; we do not remove other libraries' handlers).
    already_installed = any(
        getattr(h, "_pipeline_orchestrator_tag", None) == _LOG_HANDLER_TAG
        for h in root.handlers
    )
    if not already_installed:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler._pipeline_orchestrator_tag = _LOG_HANDLER_TAG  # type: ignore[attr-defined]
        handler.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
            )
        )
        root.addHandler(handler)
        root.setLevel(logging.INFO)
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.add_log_level,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
                structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
            ],
            logger_factory=structlog.stdlib.LoggerFactory(),
            wrapper_class=structlog.stdlib.BoundLogger,
            cache_logger_on_first_use=True,
        )


def register_tools(mcp: FastMCP) -> None:
    """Register every pipeline_* tool onto the given FastMCP instance.

    Delegates to the tools subpackage (currently a no-op stub; the 43 tools
    land in M6). This is the single wiring seam.
    """
    from pipeline_orchestrator.tools import (
        register_tools as _register_tools,  # noqa: PLC0415
    )

    _register_tools(mcp)


# ── ServerState (spec §3.2) — module-level singleton ─────────────────
#
# The TS server holds module-level mutable state (`activeRun`, `activeRunDir`,
# `activeDebate`, lazy `_config/_schemas/_qualityGates/_hooksConfig`,
# `_projectRoot`) plus a dozen DI context objects. In Python this collapses into
# a single module-level `ServerState` dataclass (singleton, importable by tool
# modules) holding the mutable attrs + lazy compute-once getters + the DI seam
# methods. The per-handler ctx objects (lifecycleCtx/artifactCtx/...) land with
# their handlers in T6.2–T6.5; T6.1 builds only ServerState + seams.


def _bundled_pipeline_dir() -> str:
    """Absolute path to the package-bundled ``pipeline/`` directory.

    Resolved relative to ``config.bundled_pipeline_toml_path()`` so it mirrors
    the existing bundling convention (OS-agnostic, CWD-independent). This is the
    Python analogue of the TS ``PIPELINE_DIR = resolve(__dirname, 'pipeline')``.
    """
    return str(_config_mod.bundled_pipeline_toml_path().parent)


def _bundled_schemas_dir() -> str:
    """Absolute path to the package-bundled ``schemas/`` directory.

    The TS uses ``join(PIPELINE_DIR, 'schemas')``; the Python schemas are bundled
    one level up at ``src/pipeline_orchestrator/schemas/`` (sibling of
    ``pipeline/``), so resolve relative to the package root.
    """
    return str(
        _config_mod.bundled_pipeline_toml_path().parent.parent / "schemas"
    )


@dataclass
class ServerState:
    """Module-level server state singleton (spec §3.2).

    Mirrors the TS module-level mutable state + lazy config caches + DI seams.
    ``active_run`` / ``active_run_dir`` / ``active_debate`` are mutable; the
    config getters compute once on first call and cache; ``project_root`` is a
    settable value (NOT a pure cache). The DI seam methods preserve the
    run-scoped-vs-legacy routing branch as a single testable surface.
    """

    active_run: RunState | None = None
    active_run_dir: str = ""
    active_debate: DebateState | None = None

    _project_root: str | None = field(default=None, repr=False)
    _config: PipelineConfig | None = field(default=None, repr=False)
    _schemas: SchemaMap | None = field(default=None, repr=False)
    _quality_gates: list[QualityGate] | None = field(default=None, repr=False)
    _hooks_config: list[Any] | None = field(default=None, repr=False)

    # ── project_root (settable, not a pure cache) ────────────────────

    def set_project_root(self, root: str) -> None:
        """Store the resolved absolute project root (TS ``setProjectRoot``).

        Resolves via ``os.path.realpath`` (the Node ``resolve()`` analogue that
        also canonicalizes symlinks) so subsequent path joins are stable.
        """
        self._project_root = os.path.realpath(root)

    def project_root(self) -> str:
        """Return the project root or raise byte-exact (TS ``getProjectRoot``)."""
        if self._project_root:
            return self._project_root
        raise ValueError(
            "project_root not set. Pass project_root in pipeline_init_run."
        )

    def project_root_or_none(self) -> str | None:
        """Return the project root or ``None`` (TS ``() => _projectRoot``).

        Several TS ctx objects expose the raw nullable ``_projectRoot`` rather
        than the throwing getter; this is that non-throwing accessor.
        """
        return self._project_root

    # ── lazy compute-once config getters ─────────────────────────────

    def config(self) -> PipelineConfig:
        """Parse + cache the bundled ``pipeline.toml`` (TS ``getConfig``)."""
        if self._config is None:
            self._config = _config_mod.pipeline_config()
        return self._config

    def config_storage_base_dir(self) -> str:
        """Return ``config().storage.base_dir``, ``""`` when storage is absent.

        The Python ``PipelineConfig.storage`` is ``StorageConfig | None`` whereas
        the TS ``getConfig().storage.base_dir`` is non-null; this guarded accessor
        mirrors the same fallback already used in :meth:`get_storage_config`, so
        DI ctx bindings can read the base dir without repeating the None guard.
        """
        storage = self.config().storage
        return storage.base_dir if storage is not None else ""

    def schemas(self) -> SchemaMap:
        """Load + cache the bundled JSON schemas (TS ``getSchemas``)."""
        if self._schemas is None:
            self._schemas = _validator_mod.load_schemas(_bundled_schemas_dir())
        return self._schemas

    def quality_gates(self) -> list[QualityGate]:
        """Load + cache the bundled quality gates (TS ``getQualityGates``)."""
        if self._quality_gates is None:
            gates_path = os.path.join(_bundled_pipeline_dir(), "quality-gates.toml")
            self._quality_gates = _toml_loader_mod.load_quality_gates(gates_path)
        return self._quality_gates

    def hooks_config(self) -> list[Any]:
        """Load + cache the bundled hooks config (TS ``getHooksConfig``)."""
        if self._hooks_config is None:
            toml_path = os.path.join(_bundled_pipeline_dir(), "pipeline.toml")
            self._hooks_config = _toml_loader_mod.load_hooks_config(toml_path)
        return self._hooks_config

    # ── DI seam methods (byte-exact vs server.ts) ────────────────────

    def base_dir_from_run_dir(self, run_dir: str) -> str:
        """Derive the storage base dir from a run dir (TS ``baseDirFromRunDir``).

        Normalize ``\\`` -> ``/``, find the last ``/runs/`` segment; if present
        return the prefix before it; else fall back to ``resolve(runDir,
        '../..')`` (two levels up).
        """
        normalised = run_dir.replace("\\", "/")
        idx = normalised.rfind("/runs/")
        if idx != -1:
            return run_dir[:idx]
        return os.path.abspath(os.path.join(run_dir, "..", ".."))

    def get_storage_config(
        self, base_override: str | None = None
    ) -> StorageConfig:
        """Build the active ``StorageConfig`` (TS ``getStorageConfig``).

        ``base`` = ``base_override`` or
        ``realpath(join(project_root(), config().storage.base_dir))``; ``paths``
        come from ``config().storage.paths``.
        """
        cfg = self.config()
        storage = cfg.storage
        base_dir = storage.base_dir if storage is not None else ""
        paths = storage.paths if storage is not None else {}
        if base_override is not None:
            base = base_override
        else:
            base = os.path.realpath(os.path.join(self.project_root(), base_dir))
        return _storage_mod.StorageConfig(base_dir=base, paths=dict(paths))

    def require_run(self) -> RunState:
        """Return the active run or raise byte-exact (TS ``requireRun``)."""
        if self.active_run is None:
            raise ValueError("No active run. Call pipeline_init_run first.")
        return self.active_run

    # ── thin delegations to storage.py / run_state.py ────────────────

    def store_artifact(
        self,
        config: StorageConfig,
        storage_key: str,
        file_name: str,
        artifact: object,
        force: bool | None = None,
    ) -> str:
        """Delegate to :func:`storage.store_artifact` (legacy flat write)."""
        return _storage_mod.store_artifact(
            config, storage_key, file_name, artifact, force
        )

    def load_artifact(
        self,
        config: StorageConfig,
        storage_key: str,
        file_name: str,
    ) -> object | None:
        """Delegate to :func:`storage.load_artifact`."""
        return _storage_mod.load_artifact(config, storage_key, file_name)

    def add_artifact(self, ref: ArtifactRef, state_dir: str) -> RunState:
        """Delegate to :func:`run_state.add_artifact` against the active run."""
        return _run_state_mod.add_artifact(self.require_run(), ref, state_dir)

    def persist_artifact(
        self,
        storage_key: str,
        file_name: str,
        artifact: object,
        force: bool = False,
    ) -> str:
        """Route an artifact write through the per-run path (TS ``persistArtifact``).

        Mirrors server.ts lines 289–298 exactly: resolve the storage key to a
        run subtype; if the key has no run-scoped subtype OR there is no
        ``run_data_dir``, raise the byte-exact contract-violation error;
        otherwise write via :func:`storage.persist_artifact`.
        """
        run_data_dir = (
            self.active_run.run_data_dir if self.active_run is not None else None
        )
        subtype = _storage_mod.storage_key_to_subtype(storage_key)
        if subtype is None or not run_data_dir:
            shown = run_data_dir or ""
            raise ValueError(
                f'persistArtifact: cannot route storageKey="{storage_key}" '
                f'with run_data_dir="{shown}" to run-scoped path'
            )
        return _storage_mod.persist_artifact(
            run_data_dir, subtype, file_name, artifact, force
        )


# The module-level singleton. Tool modules import this object directly (the
# Python analogue of the TS module-level mutable closures + ctx objects).
state = ServerState()


def main() -> None:
    """Entry point: configure logging, register tools, run over stdio.

    Config (SECOND_BRAIN_*, AGENTIC_OS_DATA, PIPELINE_* etc.) is parsed lazily
    on the first tool call that needs it rather than at import time, so an MCP
    ``initialize``/``tools/list`` handshake succeeds even with no
    ``pipeline.toml`` resolved (the tools themselves return contract-honest
    error envelopes).
    """
    configure_logging()
    register_tools(mcp)
    # Register the live `vector:second_brain` provider adapter once at startup
    # (spec §3.2 / R5 §6) — the ONLY registered adapter, keyed `vector:second_brain`.
    # Done in main() (NOT at import) so the MCP handshake stays clean. With no
    # SECOND_BRAIN_INDEX_PATH set the adapter is a no-op that returns
    # provider_not_configured without spawning anything.
    from typing import cast

    from pipeline_orchestrator.kb_client import (
        KBProviderAdapter,
        register_adapter,
    )
    from pipeline_orchestrator.second_brain_adapter import (
        create_second_brain_adapter,
    )

    register_adapter(cast("KBProviderAdapter", create_second_brain_adapter()))
    # Startup line goes to stderr only — never stdout (protocol-safe, spec §3.2).
    sys.stderr.write("pipeline: MCP server started\n")
    mcp.run()
