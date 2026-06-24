"""Phase lifecycle hook execution seam (port of ``legacy-node/hooks.ts``, spec §7.6).

Byte-exact behavioral port of the Node ``hooks.ts`` module: the subprocess
runner that fires lifecycle hooks via :func:`subprocess.run`, the path-traversal
guard, the phase-filter matcher, the ``agent_directive`` parser, the
``pre_pipeline_init`` parameter/prompt collector, and the ``post_complete``
idempotency guard keyed on ``events.jsonl``.

Parity notes carried over from the Node source:

* Hook context is passed via the ``PIPELINE_HOOK_CONTEXT`` env var, capped at
  64KB; an oversized context is replaced with a small ``context_too_large``
  marker object (NOT truncated) — preserving the Node ``MAX_CONTEXT_BYTES`` cap.
* ``timeout_ms`` defaults to ``5000``; the default is applied with
  ``.get("timeout_ms", 5000)`` semantics (the TS ``?? 5000``) so an explicit
  ``0`` would survive — but :class:`~pipeline_orchestrator.models.HookConfig`
  already fills ``5000`` via the loader.
* ``pre_start`` hooks are blocking (failures re-raise as a
  ``PipelineError('configuration_error')``); ``post_complete`` / ``on_fail``
  hooks are non-blocking (failures recorded as ``success: False`` results).
* ``runPrePipelineInitHooks`` is blocking; later hooks' ``parameters`` override
  earlier ones (``dict.update`` = ``Object.assign`` later-wins) and ``userPrompts``
  accumulate, including the legacy ``user_prompts`` / ``user_prompt`` aliases.
* Diagnostics go to **stderr** (the Node ``console.warn``) — never stdout, which
  would corrupt the JSON-RPC channel.

The :class:`HookResult` / :class:`PreInitHookResult` returns are plain dicts
keyed exactly as the Node object literals, and a parsed ``agent_directive`` is a
plain dict with the four required string fields (plus an optional
``isolation: 'worktree'``) — matching what the Node tests ``deepEqual`` against.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from .errors import ErrorClass, PipelineError
from .models import HookConfig, HookTrigger

#: 64KB cap on the ``PIPELINE_HOOK_CONTEXT`` env var (the Node ``MAX_CONTEXT_BYTES``).
MAX_CONTEXT_BYTES = 65536


@dataclass
class PreInitHookResult:
    """Result of running all ``pre_pipeline_init`` hooks (TS ``PreInitHookResult``).

    ``parameters`` is the later-wins merge of every hook's emitted ``parameters``;
    ``userPrompts`` accumulates ``userPrompts`` / ``user_prompts`` / ``user_prompt``
    across hooks. Field names match the TS object so callers/tests read identically.
    """

    parameters: dict[str, Any] = field(default_factory=dict)
    userPrompts: list[str] = field(default_factory=list)  # noqa: N815


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (Node ``toISOString()``)."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def parse_agent_directive(stdout: str | None) -> dict[str, Any] | None:
    """Parse an ``AgentDirective`` from a hook's raw stdout string.

    Mirrors ``parseAgentDirective`` in ``hooks.ts``. The stdout must be a JSON
    object containing an ``agent_directive`` field whose value has all four
    required string fields (``subagent_type`` / ``model`` / ``description`` /
    ``prompt``). An optional ``isolation: 'worktree'`` is preserved; any other
    ``isolation`` value is dropped. Returns ``None`` when stdout is absent, empty,
    not JSON, missing the field, or fails shape validation. Intentionally lenient
    so non-directive hooks pass through unchanged.
    """
    if not stdout:
        return None
    trimmed = stdout.strip()
    if len(trimmed) == 0:
        return None

    try:
        parsed: Any = json.loads(trimmed)
    except (json.JSONDecodeError, ValueError):
        return None

    if not parsed or not isinstance(parsed, dict):
        return None
    raw = parsed.get("agent_directive")
    if not raw or not isinstance(raw, dict):
        return None

    subagent_type = raw.get("subagent_type")
    model = raw.get("model")
    description = raw.get("description")
    prompt = raw.get("prompt")
    if (
        not isinstance(subagent_type, str)
        or not isinstance(model, str)
        or not isinstance(description, str)
        or not isinstance(prompt, str)
    ):
        return None

    directive: dict[str, Any] = {
        "subagent_type": subagent_type,
        "model": model,
        "description": description,
        "prompt": prompt,
    }
    if raw.get("isolation") == "worktree":
        directive["isolation"] = "worktree"
    return directive


def validate_hook_path(command: str, project_root: str) -> str:
    """Validate that a hook command path is safe and resolve it under ``project_root``.

    Mirrors ``validateHookPath`` in ``hooks.ts``:

    * Normalize the command path.
    * Reject any path containing ``..`` after normalization
      (``configuration_error``).
    * Resolve relative paths against ``project_root``; leave absolute paths as-is.
    * Verify the resolved path begins with the normalized project root
      (``configuration_error`` otherwise).

    Uses ``os.path.normpath`` (the Node ``path.normalize`` analogue) and
    ``os.path.isabs`` / ``os.path.join`` so behavior matches per-platform path
    semantics, exactly like the Node source.
    """
    normalized = os.path.normpath(command)

    # Reject paths with '..' traversal after normalization. Mirrors the Node
    # ``normalized.includes('..')`` — a substring check (intentionally broad:
    # it also rejects a filename that merely contains ".." as a substring),
    # preserved verbatim for byte-exact parity with hooks.ts.
    if ".." in normalized:
        raise PipelineError(
            f'Hook command path "{command}" contains directory traversal (..)',
            ErrorClass.configuration_error,
            recovery_action=(
                'Use a path relative to the project root without ".." components.'
            ),
            details={"command": command, "normalized": normalized},
        )

    # Resolve against project root.
    resolved = (
        normalized
        if os.path.isabs(normalized)
        else os.path.normpath(os.path.join(project_root, normalized))
    )

    # Verify resolved path is under project root.
    normalized_root = os.path.normpath(project_root)
    if not resolved.startswith(normalized_root):
        raise PipelineError(
            f'Hook command path "{command}" resolves outside project root',
            ErrorClass.configuration_error,
            recovery_action="Ensure hook command path is within the project directory.",
            details={
                "command": command,
                "resolved": resolved,
                "projectRoot": project_root,
            },
        )

    return resolved


def matches_phase(filter_: str | None, phase_name: str) -> bool:
    """Check whether a hook's ``phase_filter`` matches the given phase name.

    Mirrors ``matchesPhase`` in ``hooks.ts``: ``'*'`` (or absent) matches all
    phases; otherwise an exact string match is required.
    """
    if not filter_ or filter_ == "*":
        return True
    return filter_ == phase_name


def _has_phase_completed_event(run_dir: str, phase: str) -> bool:
    """Return whether ``events.jsonl`` already has a ``phase_completed`` for ``phase``.

    Mirrors ``hasPhaseCompletedEvent`` in ``hooks.ts``. Returns ``False`` when the
    file is missing/unreadable or no matching line is present; malformed JSON
    lines are skipped defensively.
    """
    try:
        events_path = os.path.join(run_dir, "events.jsonl")
        with open(events_path, encoding="utf-8") as fh:
            content = fh.read()
    except OSError:
        # File missing or unreadable — treat as "no prior event".
        return False

    for line in content.split("\n"):
        trimmed = line.strip()
        if not trimmed:
            continue
        try:
            parsed = json.loads(trimmed)
        except (json.JSONDecodeError, ValueError):
            # Malformed line — skip defensively.
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("event") == "phase_completed"
            and parsed.get("phase") == phase
        ):
            return True
    return False


def _execute_hook(
    resolved_command: str, args: list[str], timeout_ms: int, env_context: str
) -> str:
    """Run a hook subprocess and return its stdout, raising on failure/timeout.

    Mirrors the Node ``execFileSync(..., { timeout, env, encoding, stdio })``:
    inherits the process env with ``PIPELINE_HOOK_CONTEXT`` overlaid, captures
    stdout/stderr as text, and raises :class:`subprocess.CalledProcessError`
    (non-zero exit, carrying ``.stderr``) or :class:`subprocess.TimeoutExpired`.
    """
    env = {**os.environ, "PIPELINE_HOOK_CONTEXT": env_context}
    completed = subprocess.run(  # noqa: S603
        [resolved_command, *args],
        capture_output=True,
        text=True,
        timeout=timeout_ms / 1000,
        env=env,
        check=True,
    )
    return completed.stdout


def run_hooks(
    hooks: list[HookConfig],
    trigger: HookTrigger,
    context: dict[str, Any],
    project_root: str,
) -> list[dict[str, Any]]:
    """Run hooks matching a specific trigger and phase.

    Mirrors ``runHooks`` in ``hooks.ts``. ``pre_start`` hooks are blocking (a
    failure re-raises a ``PipelineError('configuration_error')``);
    ``post_complete`` / ``on_fail`` hooks are non-blocking (failures recorded as
    ``success: False`` results). For ``post_complete``, an idempotency guard short
    -circuits when ``events.jsonl`` already records a ``phase_completed`` for the
    target phase, returning a synthetic skipped result per matching hook.

    ``context`` is the ``HookContext`` dict (``run_id`` / ``phase`` / ``trigger`` /
    ``project_root`` / ``run_dir`` plus any extra keys); each returned result is a
    plain dict keyed exactly as the Node ``HookResult`` object literal.
    """
    phase = context["phase"]
    matching = [
        h
        for h in hooks
        if h.trigger == trigger and matches_phase(h.phase_filter, phase)
    ]

    if len(matching) == 0:
        return []

    # Phase idempotency for post_complete.
    if trigger == "post_complete" and _has_phase_completed_event(
        context["run_dir"], phase
    ):
        timestamp = _now_iso()
        # Stderr debug line for post-mortem investigation of hook-fire sequences.
        print(
            f'[hooks] SKIPPED post_complete for phase="{phase}" at {timestamp} '
            "(events.jsonl already contains phase_completed for this phase)",
            file=sys.stderr,
        )
        return [
            {
                "command": hook.command,
                "trigger": trigger,
                "phase": phase,
                "success": False,
                "error": (
                    "skipped: phase_completed already recorded for phase "
                    f'"{phase}" (idempotency guard)'
                ),
            }
            for hook in matching
        ]

    # Debug logging keyed on trigger + phase + timestamp for post-mortem traces.
    print(
        f'[hooks] firing trigger="{trigger}" phase="{phase}" '
        f"matched={len(matching)} at {_now_iso()}",
        file=sys.stderr,
    )

    results: list[dict[str, Any]] = []
    context_json = json.dumps(context, separators=(",", ":"))

    # Cap context size for env var safety.
    if len(context_json) <= MAX_CONTEXT_BYTES:
        env_context = context_json
    else:
        env_context = json.dumps(
            {
                "error": "context_too_large",
                "run_id": context["run_id"],
                "phase": phase,
            },
            separators=(",", ":"),
        )

    for hook in matching:
        try:
            resolved_command = validate_hook_path(hook.command, project_root)
        except PipelineError as err:
            result: dict[str, Any] = {
                "command": hook.command,
                "trigger": trigger,
                "phase": phase,
                "success": False,
                "error": err.message,
            }
            if trigger == "pre_start":
                raise  # Re-throw for blocking hooks.
            results.append(result)
            continue

        try:
            stdout = _execute_hook(
                resolved_command,
                hook.args,
                hook.timeout_ms,
                env_context,
            )
            trimmed_stdout = stdout.strip() if isinstance(stdout, str) else None
            directive = parse_agent_directive(trimmed_stdout)
            ok_result: dict[str, Any] = {
                "command": hook.command,
                "trigger": trigger,
                "phase": phase,
                "success": True,
                "stdout": trimmed_stdout,
            }
            if directive:
                ok_result["agent_directive"] = directive
            results.append(ok_result)
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as err:
            message = str(err)
            stderr = getattr(err, "stderr", None)
            err_result: dict[str, Any] = {
                "command": hook.command,
                "trigger": trigger,
                "phase": phase,
                "success": False,
                "stderr": stderr,
                "error": message,
            }

            if trigger == "pre_start":
                # Blocking: throw PipelineError.
                raise PipelineError(
                    f'Pre-start hook failed for phase "{phase}": '
                    f"{hook.command} — {message}",
                    ErrorClass.configuration_error,
                    recovery_action=(
                        "Fix the hook script or remove it from hooks configuration."
                    ),
                    details={
                        "command": hook.command,
                        "phase": phase,
                        "error": message,
                    },
                ) from err

            # Non-blocking: record and continue.
            results.append(err_result)

    return results


def run_pre_pipeline_init_hooks(
    hooks: list[HookConfig],
    context: dict[str, Any],
    project_root: str,
) -> PreInitHookResult:
    """Run all ``pre_pipeline_init`` hooks in order, merging parameters and prompts.

    Mirrors ``runPrePipelineInitHooks`` in ``hooks.ts``. Each hook's stdout must
    be a JSON object shaped ``{ parameters?, userPrompts? }`` (plus legacy
    ``user_prompts`` / ``user_prompt`` aliases). Later hooks' ``parameters``
    override earlier ones; prompts accumulate. Blocking: any hook failure
    (non-zero exit, timeout, malformed JSON, path traversal) raises a
    ``PipelineError('configuration_error')``. Returns an empty result when no
    ``pre_pipeline_init`` hooks exist.
    """
    matching = [h for h in hooks if h.trigger == "pre_pipeline_init"]
    merged: dict[str, Any] = {}
    all_prompts: list[str] = []

    if len(matching) == 0:
        return PreInitHookResult(parameters=merged, userPrompts=all_prompts)

    print(
        f'[hooks] firing trigger="pre_pipeline_init" '
        f"matched={len(matching)} at {_now_iso()}",
        file=sys.stderr,
    )

    context_json = json.dumps(context, separators=(",", ":"))
    if len(context_json) <= MAX_CONTEXT_BYTES:
        env_context = context_json
    else:
        env_context = json.dumps(
            {"error": "context_too_large", "trigger": "pre_pipeline_init"},
            separators=(",", ":"),
        )

    for hook in matching:
        # Re-throw path-traversal / outside-root errors as configuration_error.
        resolved_command = validate_hook_path(hook.command, project_root)

        try:
            stdout = _execute_hook(
                resolved_command,
                hook.args,
                hook.timeout_ms,
                env_context,
            )
        except (
            subprocess.CalledProcessError,
            subprocess.TimeoutExpired,
            OSError,
        ) as err:
            message = str(err)
            raise PipelineError(
                f"pre_pipeline_init hook failed: {hook.command} — {message}",
                ErrorClass.configuration_error,
                recovery_action=(
                    "Fix the hook script or remove it from the [[hooks]] configuration."
                ),
                details={"command": hook.command, "error": message},
            ) from err

        trimmed = stdout.strip() if isinstance(stdout, str) else ""
        if len(trimmed) == 0:
            continue

        try:
            parsed: Any = json.loads(trimmed)
        except (json.JSONDecodeError, ValueError) as json_err:
            raise PipelineError(
                f'pre_pipeline_init hook "{hook.command}" emitted non-JSON stdout: '
                f"{json_err}",
                ErrorClass.configuration_error,
                recovery_action=(
                    "Ensure the hook script writes a JSON object to stdout with "
                    "{ parameters, userPrompts } shape."
                ),
                details={
                    "command": hook.command,
                    "stdout_preview": trimmed[:200],
                },
            ) from json_err

        if not isinstance(parsed, dict):
            parsed = {}

        params = parsed.get("parameters")
        if params and isinstance(params, dict):
            merged.update(params)
        user_prompts = parsed.get("userPrompts")
        if isinstance(user_prompts, list):
            for p in user_prompts:
                if isinstance(p, str):
                    all_prompts.append(p)
        legacy_prompts = parsed.get("user_prompts")
        if isinstance(legacy_prompts, list):
            for p in legacy_prompts:
                if isinstance(p, str):
                    all_prompts.append(p)
        legacy_single = parsed.get("user_prompt")
        if isinstance(legacy_single, str):
            all_prompts.append(legacy_single)

    return PreInitHookResult(parameters=merged, userPrompts=all_prompts)
