"""Core data models (spec §3.3, R2 §4) — dataclass **stubs** for M0.

These are intentionally empty-but-importable shells. The real fields
(``RunState``/``PhaseState``/``ArtifactRef``/``LifecycleEvent``) are filled in
M1 (T1.2) when ``run_state.py`` is ported from ``run-state.ts``. They exist now
only so the package imports cleanly and downstream modules can name the types.
Each must import cleanly and pass mypy strict.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class ArtifactRef:
    """Reference to a stored artifact (shell — fields filled in M1)."""


@dataclass
class LifecycleEvent:
    """A run/phase lifecycle event (shell — fields filled in M1)."""


@dataclass
class PhaseState:
    """Per-phase state within a run (shell — fields filled in M1)."""


@dataclass
class RunState:
    """Top-level run state (shell — fields filled in M1)."""
