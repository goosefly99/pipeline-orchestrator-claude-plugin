"""Debate MCP tool bodies (port of ``legacy-node/debate-handlers.ts``).

The four ``handle_*_debate`` / ``handle_submit_argument`` handlers take a
:class:`DebateContext` (or :class:`SaveDebateContext`) dependency object — the DI
seam ``server.py`` wires to the real run-state / storage / validator functions —
and return a :class:`~pipeline_orchestrator.models.HandlerResponse`. All four
debate tool outputs are **JSON** (``json.dumps(obj, indent=2,
ensure_ascii=False)``) except ``save_debate``'s validation-failure branch, which
returns a **plain-text** error block with ``is_error=True`` (§5.3 "J / T fail").

:func:`register_debate_tools` registers the four ``pipeline_*_debate`` /
``pipeline_submit_argument`` tools onto a FastMCP instance. All four are
identical in shape: ``ToolAnnotations(destructiveHint=True)`` +
``_meta.max_result_chars = 10000`` (§5.3). It is NOT called from anywhere yet:
the global ``register_tools`` seam (``tools/__init__.py``) stays a no-op until
the wiring task (T6.5).

Server-level ``active_debate`` state lives at module level (:data:`ACTIVE_DEBATE`,
mirroring ``collections_store.STORE``): ``handle_init_debate`` sets it via the
context's ``set_active_debate``; ``handle_save_debate`` clears it after
validating + persisting. Tests inject a stubbed context to isolate state, exactly
as the TS ``debate-handlers.test.ts`` passes a stub ``ctx``.

Parity notes:

* **Wire format.** Init / submit / synthesize / save (success) return
  ``json.dumps(obj, indent=2, ensure_ascii=False)`` — the indented JSON Node's
  ``JSON.stringify(obj, null, 2)`` emits — with the field order matching the TS
  object literal. ``save_debate``'s validation-failure branch returns the plain
  ``"Debate transcript validation FAILED:\n{errors}"`` text block (``is_error``).
* **``ensure_ascii=False``** on every ``json.dumps`` so em/en-dashes and accented
  characters survive raw (Node ``JSON.stringify`` emits raw UTF-8).
* **Nullish, not falsy.** The ``synthesize_debate`` branch keys on the
  *presence* of ``changes_accepted`` (the TS ``if (!args.changes_accepted)``):
  an absent key returns the prompt; a present (even empty-array) value records
  the synthesis. ``kb_queries_used ?? false`` ports to ``is None`` coalesce.
* **Required-field guards** reproduce the TS truthiness checks byte-for-byte
  (``input_type``/``input_id``/``input_version``/``artifact_content`` for init;
  ``role``/``position``/``confidence is None`` for submit; ``file_name``/``phase``
  for save).
* **Diagnostics → stderr.** No handler prints to stdout (MCP stdio is the
  JSON-RPC channel); there is no diagnostic output in these handlers, matching
  the TS (which is likewise silent).
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mcp.types import ToolAnnotations

from pipeline_orchestrator.debate import (
    build_transcript,
    generate_agent_prompts,
    generate_synthesis_prompt,
    init_debate,
    submit_argument,
)
from pipeline_orchestrator.models import (
    AgentArgument,
    AgentRole,
    ArtifactRef,
    DebateInputType,
    DebateState,
    DebateSynthesis,
    DomainProfile,
    HandlerResponse,
    RunState,
)
from pipeline_orchestrator.validator import SchemaMap, ValidationResult

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


# ── Server-level active-debate state (mirrors collections_store.STORE) ──

ACTIVE_DEBATE: DebateState | None = None
"""The single in-flight debate (the server-level ``activeDebate`` in ``server.ts``).

``server.py`` (T6.5) wires :class:`DebateContext.get_active_debate` /
``set_active_debate`` to read/write this module global. Tests inject their own
getters/setters into a stub context to isolate state.
"""


def get_active_debate() -> DebateState | None:
    """Read the module-level :data:`ACTIVE_DEBATE` (default context getter)."""
    return ACTIVE_DEBATE


def set_active_debate(state: DebateState | None) -> None:
    """Write the module-level :data:`ACTIVE_DEBATE` (default context setter)."""
    global ACTIVE_DEBATE
    ACTIVE_DEBATE = state


# ── Context provided by server.py (the DI seam) ──────────────────────


@dataclass
class DebateContext:
    """Dependency object the debate handlers call through (TS ``DebateContext``).

    ``get_active_debate`` / ``set_active_debate`` read/write the server-level
    active debate; ``server.py`` wires them to the module-level
    :data:`ACTIVE_DEBATE`, while tests inject stubs that capture a local store.
    """

    get_active_debate: Callable[[], DebateState | None]
    set_active_debate: Callable[[DebateState | None], None]


@dataclass
class SaveDebateContext(DebateContext):
    """The richer context ``handle_save_debate`` needs (TS ``SaveDebateContext``).

    Adds the validator / storage / run-state seam. ``persist_debate`` is the
    ``?:`` optional (``None`` when a test fixture omits it):
    ``handle_save_debate`` prefers ``persist_debate`` (Feature-C run-scoped
    routing) when present and otherwise falls back to ``store_artifact`` via the
    legacy base-dir path. Both branches preserve the
    throw-on-collision-unless-force semantics of ``store_artifact``.
    """

    validate_artifact: Callable[[SchemaMap, str, object], ValidationResult]
    get_schemas: Callable[[], SchemaMap]
    store_artifact: Callable[..., str]
    get_storage_config: Callable[[str], object]
    base_dir_from_run_dir: Callable[[str], str]
    get_project_root: Callable[[], str]
    get_config_storage_base_dir: Callable[[], str]
    get_active_run: Callable[[], RunState | None]
    get_active_run_dir: Callable[[], str]
    add_artifact: Callable[[RunState, ArtifactRef, str], RunState]
    set_active_run: Callable[[RunState], None]
    persist_debate: Callable[..., str] | None = None


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string (Node ``toISOString()``)."""
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ── Handlers ─────────────────────────────────────────────────────────


def handle_init_debate(
    args: dict[str, object], ctx: DebateContext
) -> HandlerResponse:
    """Start a debate; set active state; return the 4 round-1 agent prompts.

    Port of TS ``handleInitDebate``. The response embeds ``transcript_id`` /
    ``input_type`` / ``input_id`` / ``instruction`` / ``agents``, plus
    ``artifact_path`` only when one was supplied (the artifact-once pattern: the
    agent prompts never embed the artifact JSON).
    """
    input_type = args.get("input_type")
    input_id = args.get("input_id")
    input_version = args.get("input_version")
    artifact_content = args.get("artifact_content")
    artifact_path = args.get("artifact_path")
    codebase_requirements_id = args.get("codebase_requirements_id")
    domain_profile = args.get("domain_profile")

    if not input_type or not input_id or not input_version or not artifact_content:
        raise ValueError(
            "input_type, input_id, input_version, and artifact_content are required"
        )

    assert isinstance(input_type, str)
    assert isinstance(input_id, str)
    assert isinstance(input_version, str)
    assert artifact_path is None or isinstance(artifact_path, str)
    assert codebase_requirements_id is None or isinstance(
        codebase_requirements_id, str
    )
    assert (
        domain_profile is None
        or isinstance(domain_profile, str)
        or isinstance(domain_profile, DomainProfile)
    )

    state = init_debate(
        input_type=input_type,  # type: ignore[arg-type]
        input_id=input_id,
        input_version=input_version,
        artifact_content=artifact_content,
        artifact_path=artifact_path,
        codebase_requirements_id=codebase_requirements_id,
        domain_profile=domain_profile,
    )
    ctx.set_active_debate(state)
    agent_prompts = generate_agent_prompts(state)

    response: dict[str, object] = {
        "transcript_id": state.transcript_id,
        "input_type": state.input_type,
        "input_id": state.input_id,
        "instruction": " ".join(
            [
                "Dispatch all 4 agents in parallel using the Agent tool.",
                "Combine artifact_path with each agent role prompt when dispatching.",
                "Feed each agent its role prompt plus the artifact at artifact_path. "
                "Parse the JSON response from each agent.",
                "Call pipeline_submit_argument once per agent with the parsed result.",
                "Then call pipeline_synthesize_debate to get the synthesis prompt.",
            ]
        ),
        "agents": [
            {"role": p.role, "prompt": p.prompt} for p in agent_prompts
        ],
    }

    if artifact_path is not None:
        response["artifact_path"] = artifact_path

    return HandlerResponse(
        json=json.dumps(response, indent=2, ensure_ascii=False)
    )


def handle_submit_argument(
    args: dict[str, object], ctx: DebateContext
) -> HandlerResponse:
    """Record one round-1 argument; report progress (TS ``handleSubmitArgument``).

    Coalesces the three optional arrays to ``[]`` only when not a list (the TS
    ``Array.isArray(...) ? ... : []``), guards the three required fields, then
    reports the submitted/remaining roles and the next step.
    """
    active_debate = ctx.get_active_debate()
    if not active_debate:
        raise ValueError("No active debate. Call pipeline_init_debate first.")

    role = args.get("role")
    position = args.get("position")
    confidence = args.get("confidence")
    evidence = args.get("evidence")
    counterpoints = args.get("counterpoints")
    proposed_changes = args.get("proposed_changes")

    evidence_list = evidence if isinstance(evidence, list) else []
    counterpoints_list = counterpoints if isinstance(counterpoints, list) else []
    proposed_changes_list = (
        proposed_changes if isinstance(proposed_changes, list) else []
    )

    if not role or not position or confidence is None:
        raise ValueError("role, position, and confidence are required")

    assert isinstance(role, str)
    assert isinstance(position, str)
    assert isinstance(confidence, (int, float))

    updated = submit_argument(
        active_debate,
        AgentArgument(
            role=role,  # type: ignore[arg-type]
            position=position,
            confidence=confidence,
            evidence=evidence_list,
            counterpoints=counterpoints_list,
            proposed_changes=proposed_changes_list,
        ),
    )
    ctx.set_active_debate(updated)

    submitted_roles = [a.role for a in updated.round1_arguments]
    all_round1: list[AgentRole] = [
        "advocate",
        "critic",
        "risk_specialist",
        "domain_specialist",
    ]
    remaining = [r for r in all_round1 if r not in submitted_roles]

    next_step = (
        "All round-1 arguments recorded. Call pipeline_synthesize_debate to get "
        "the synthesizer prompt."
        if len(remaining) == 0
        else f"Still waiting for: {', '.join(remaining)}"
    )

    return HandlerResponse(
        json=json.dumps(
            {
                "recorded": role,
                "submitted_so_far": submitted_roles,
                "remaining": remaining,
                "ready_for_synthesis": len(remaining) == 0,
                "next_step": next_step,
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def handle_synthesize_debate(
    args: dict[str, object], ctx: DebateContext
) -> HandlerResponse:
    """Two-step synthesis (port of TS ``handleSynthesizeDebate``).

    First call (``changes_accepted`` absent/falsy) returns the synthesizer agent
    prompt; second call (``changes_accepted`` present) records the synthesis on
    the active debate. Branches on the *presence* of ``changes_accepted`` exactly
    as the TS ``if (!args.changes_accepted)``.
    """
    active_debate = ctx.get_active_debate()
    if not active_debate:
        raise ValueError("No active debate. Call pipeline_init_debate first.")

    if not args.get("changes_accepted"):
        prompt = generate_synthesis_prompt(active_debate)
        response_obj: dict[str, object] = {
            "instruction": (
                "Dispatch the Synthesizer agent using the Agent tool with this "
                "prompt. Parse the JSON response, then call "
                "pipeline_synthesize_debate again with the parsed changes_accepted, "
                "changes_rejected, and open_questions fields."
            ),
            "synthesis_prompt": prompt,
        }
        if active_debate.artifact_path is not None:
            response_obj["artifact_path"] = active_debate.artifact_path
        return HandlerResponse(
            json=json.dumps(response_obj, indent=2, ensure_ascii=False)
        )

    changes_accepted = args.get("changes_accepted")
    changes_rejected = args.get("changes_rejected")
    open_questions = args.get("open_questions")
    synthesis = DebateSynthesis(
        changes_accepted=(
            changes_accepted if isinstance(changes_accepted, list) else []
        ),
        changes_rejected=(
            changes_rejected if isinstance(changes_rejected, list) else []
        ),
        open_questions=(open_questions if isinstance(open_questions, list) else []),
    )
    output_version = args.get("output_version")
    kb_queries_used_arg = args.get("kb_queries_used")
    kb_queries_used = (
        kb_queries_used_arg if kb_queries_used_arg is not None else False
    )
    assert isinstance(kb_queries_used, bool)

    updated = DebateState(
        transcript_id=active_debate.transcript_id,
        input_type=active_debate.input_type,
        input_id=active_debate.input_id,
        input_version=active_debate.input_version,
        output_version=output_version if output_version is not None else None,  # type: ignore[arg-type]
        created_date=active_debate.created_date,
        artifact_content=active_debate.artifact_content,
        artifact_path=active_debate.artifact_path,
        round1_arguments=active_debate.round1_arguments,
        synthesis=synthesis,
        codebase_requirements_id=active_debate.codebase_requirements_id,
        kb_queries_used=kb_queries_used,
        domain_profile=active_debate.domain_profile,
    )
    ctx.set_active_debate(updated)

    return HandlerResponse(
        json=json.dumps(
            {
                "status": "synthesis_recorded",
                "changes_accepted": len(synthesis.changes_accepted),
                "changes_rejected": len(synthesis.changes_rejected),
                "open_questions": (
                    len(synthesis.open_questions)
                    if synthesis.open_questions is not None
                    else 0
                ),
                "next_step": (
                    "Call pipeline_save_debate to validate and persist the "
                    "transcript."
                ),
            },
            indent=2,
            ensure_ascii=False,
        )
    )


def handle_save_debate(
    args: dict[str, object], ctx: SaveDebateContext
) -> HandlerResponse:
    """Validate + persist the transcript; clear active state (TS ``handleSaveDebate``).

    Builds the transcript, validates it against ``debate-transcript.json`` (the
    failure branch returns the plain-text error block with ``is_error=True``),
    persists it (preferring ``ctx.persist_debate`` Feature-C routing, else the
    legacy ``store_artifact`` path), records the artifact on the active run when
    present, clears the active debate, and returns the saved-status JSON.
    """
    active_debate = ctx.get_active_debate()
    if not active_debate:
        raise ValueError("No active debate. Call pipeline_init_debate first.")

    file_name = args.get("file_name")
    phase = args.get("phase")
    if not file_name or not phase:
        raise ValueError("file_name and phase are required")
    assert isinstance(file_name, str)
    assert isinstance(phase, str)

    transcript = build_transcript(active_debate)

    validation_result = ctx.validate_artifact(
        ctx.get_schemas(), "debate-transcript.json", transcript
    )
    if not validation_result.valid:
        return HandlerResponse(
            json="Debate transcript validation FAILED:\n"
            + "\n".join(validation_result.errors),
            is_error=True,
        )

    active_run = ctx.get_active_run()
    active_run_dir = ctx.get_active_run_dir()

    # Feature C: prefer persist_debate (run-scoped) when the context supplies it;
    # fall back to the legacy store_artifact path otherwise. Both branches
    # preserve the throw-on-collision-unless-force semantics of store_artifact.
    if ctx.persist_debate is not None:
        stored_path = ctx.persist_debate(transcript, file_name)
    else:
        base_dir = (
            ctx.base_dir_from_run_dir(active_run_dir)
            if active_run
            else os.path.join(
                ctx.get_project_root(), ctx.get_config_storage_base_dir()
            )
        )
        sc = ctx.get_storage_config(base_dir)
        stored_path = ctx.store_artifact(sc, "debates", file_name, transcript)

    if active_run:
        updated_run = ctx.add_artifact(
            active_run,
            ArtifactRef(
                type="debate-transcript",
                path=stored_path,
                phase=phase,
                created_at=_now_iso(),
                version=1,
            ),
            active_run_dir,
        )
        ctx.set_active_run(updated_run)

    saved_id = active_debate.transcript_id
    ctx.set_active_debate(None)

    return HandlerResponse(
        json=json.dumps(
            {
                "transcript_id": saved_id,
                "stored_path": stored_path,
                "status": "saved",
            },
            indent=2,
            ensure_ascii=False,
        )
    )


# ── Tool registration (NOT wired into the global seam yet — T6.5) ────

_MUTATE_MAX = 10000


def register_debate_tools(mcp: FastMCP) -> None:
    """Register the four debate tools onto ``mcp`` (spec §5.3).

    All four (``pipeline_init_debate`` / ``pipeline_submit_argument`` /
    ``pipeline_synthesize_debate`` / ``pipeline_save_debate``) are identical in
    shape: ``ToolAnnotations(destructiveHint=True)`` + ``_meta.max_result_chars =
    10000``. The bodies are thin shells over the ``handle_*`` handlers; the real
    DI context is wired by the global seam later (T6.5) — this function only
    makes the tools enumerate correctly on ``tools/list``.

    NOT called from ``tools/__init__.py`` yet: the global ``register_tools``
    handshake stays at 0 tools until the wiring task.
    """

    @mcp.tool(
        name="pipeline_init_debate",
        description=(
            "Start an adversarial debate on an artifact (knowledge-overview or "
            "design-spec). Initialises debate state and returns agent prompts for "
            "parallel dispatch."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_init_debate(
        input_type: DebateInputType,
        input_id: str,
        input_version: str,
        artifact_content: dict[str, object],
        artifact_path: str | None = None,
        codebase_requirements_id: str | None = None,
        domain_profile: object = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_submit_argument",
        description="Record one round-1 agent argument in the active debate.",
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_submit_argument(
        role: AgentRole,
        position: str,
        confidence: float,
        evidence: list[str] | None = None,
        counterpoints: list[str] | None = None,
        proposed_changes: list[str] | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_synthesize_debate",
        description=(
            "Two-step: first call (no changes_accepted) returns the synthesizer "
            "agent prompt. Second call (with changes_accepted) records the "
            "synthesis result."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_synthesize_debate(
        changes_accepted: list[str],
        changes_rejected: list[dict[str, str]],
        open_questions: list[str] | None = None,
        output_version: str | None = None,
        kb_queries_used: bool | None = None,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")

    @mcp.tool(
        name="pipeline_save_debate",
        description=(
            "Validate and save the completed debate transcript to storage. Clears "
            "active debate state."
        ),
        annotations=ToolAnnotations(destructiveHint=True),
        meta={"max_result_chars": _MUTATE_MAX},
    )
    def pipeline_save_debate(
        file_name: str,
        phase: str,
    ) -> str:
        raise NotImplementedError("wired by the global register_tools seam (T6.5)")
