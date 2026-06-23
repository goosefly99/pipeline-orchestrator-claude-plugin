"""Error model (spec §4.1) — the cross-cutting ``PipelineError`` + ``ErrorClass``.

Ported from the error half of the Node ``types.ts``. ``to_dict()`` produces a
plain JSON-able dict whose ``json.dumps`` is **single-line** (NOT pretty-printed)
— the envelope wrapper (M6, ``tools/_envelope.py``) serializes a raised
``PipelineError`` with ``json.dumps(err.to_dict())`` (no ``indent``).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorClass(StrEnum):
    """The 9 frozen error classes (spec §4.1).

    A ``StrEnum`` (the 3.11+ idiom for the spec's ``class ErrorClass(str, Enum)``):
    each member IS its string value, so ``json.dumps`` of ``member`` / ``.value``
    yields the frozen wire string.
    """

    validation_error = "validation_error"
    state_transition_error = "state_transition_error"
    file_io_error = "file_io_error"
    schema_mismatch = "schema_mismatch"
    artifact_not_found = "artifact_not_found"
    phase_not_ready = "phase_not_ready"
    configuration_error = "configuration_error"
    gate_blocked = "gate_blocked"
    unknown_error = "unknown_error"


class PipelineError(Exception):
    """Structured pipeline error carrying an error class + recovery metadata.

    Mirrors the Node ``PipelineError`` shape. ``to_dict()`` is the wire form used
    by the envelope layer; it serializes to a single-line JSON object with the
    five frozen keys ``error_class``, ``message``, ``recovery_action``,
    ``retryable``, ``details``.
    """

    def __init__(
        self,
        message: str,
        error_class: ErrorClass,
        *,
        recovery_action: str = "",
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class
        self.recovery_action = recovery_action
        self.retryable = retryable
        self.details = details if details is not None else {}
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-able envelope dict (single-line when ``json.dumps``-ed)."""
        return {
            "error_class": self.error_class.value,
            "message": self.message,
            "recovery_action": self.recovery_action,
            "retryable": self.retryable,
            "details": self.details,
        }
