"""Adversarial-debate engine (port of ``legacy-node/debate.ts``).

Byte-exact behavioral port of the Node ``debate.ts`` module: the four built-in
:class:`~pipeline_orchestrator.models.DomainProfile` constants + ``resolve_profile``;
the pure state transitions (``init_debate`` / ``submit_argument``); the transcript
assembler (``build_transcript``); the three prompt builders (``generate_agent_prompts``
/ ``build_artifact_structure_summary`` / ``generate_synthesis_prompt``); and the
Feature-C per-run debate-write helpers (``resolve_debates_dir`` / ``persist_debate``).

Parity notes:

* **``transcript_id``** = ``dt-${randomBytes(4).toString('hex')}`` →
  ``f"dt-{secrets.token_hex(4)}"`` (the ``dt-`` prefix + 8 lowercase hex chars,
  length 11).
* **``created_date``** = ``new Date().toISOString()`` → a millisecond-precision
  ``Z``-suffixed UTC timestamp (:func:`_now_iso`, the sibling-module helper).
* **JSON on-disk serialization** mirrors Node ``JSON.stringify(x, null, 2)``
  (``json.dumps(x, indent=2, ensure_ascii=False)``). ``ensure_ascii=False`` is
  required to match Node's raw-UTF-8 ``JSON.stringify``: transcript values can
  carry em/en-dashes and accented chars, which Python's default
  ``ensure_ascii=True`` would ``\\uXXXX``-escape — an unconditional byte-level
  divergence.
* **``??`` nullish defaults** are ported as explicit ``is None`` / ``or []``-free
  coalesce. The ``arg.evidence ?? []`` (etc.) on submit becomes an explicit
  ``[] if x is None else x`` so an explicit empty array survives unchanged and a
  caller-supplied populated list is preserved.
* **Transcript key order** reproduces the TS object-literal insertion order
  exactly: ``transcript_id, input_type, input_id, created_date, kb_queries_used,
  rounds, synthesis`` then the four conditionally-appended keys
  (``input_version`` / ``output_version`` / ``codebase_requirements_id`` /
  ``domain_profile``) in that order — V8 and CPython both honor insertion order,
  so the on-disk byte layout matches.
* **``domain_profile`` key** is appended only when the resolved profile name is
  not ``"default"`` (the TS ``state.domainProfile.name !== 'default'`` guard).
* **``output_version`` falsy guard.** The TS appends ``output_version`` only when
  truthy (``if (state.outputVersion)``); ``null`` and ``""`` are both dropped.
  Reproduced with an explicit truthiness check matching the TS semantics.
"""

from __future__ import annotations

import json
import os
import secrets
from datetime import UTC, datetime

from pipeline_orchestrator.models import (
    AgentArgument,
    AgentPrompt,
    AgentRole,
    DebateInputType,
    DebateState,
    DomainProfile,
)
from pipeline_orchestrator.storage import get_artifact_dir


def _now_iso() -> str:
    """Return the current UTC time as an ISO 8601 string.

    Mirrors Node ``new Date().toISOString()`` — a millisecond-precision,
    ``Z``-suffixed UTC timestamp (e.g. ``2026-04-10T12:34:56.789Z``).
    """
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


# ── Domain profiles ───────────────────────────────────────────────────

FINANCE_PROFILE = DomainProfile(
    name="finance",
    risk_vocabulary=[
        "execution risk",
        "liquidity risk",
        "model risk",
        "operational risk",
        "tail risk",
        "counterparty risk",
    ],
    domain_constraints=[
        "market microstructure",
        "platform API constraints (rate limits, settlement timing, resolution rules)",
        "regulatory requirements",
    ],
    specialist_focus=(
        "Apply deep domain knowledge: market microstructure, platform API "
        "constraints (rate limits, settlement timing, resolution rules), "
        "regulatory requirements, and operational realities."
    ),
)

SOFTWARE_PROFILE = DomainProfile(
    name="software",
    risk_vocabulary=[
        "technical debt",
        "API stability",
        "performance regression",
        "backward compatibility",
        "security vulnerability",
    ],
    domain_constraints=[
        "dependency management",
        "deployment constraints",
        "backward compatibility requirements",
    ],
    specialist_focus=(
        "Apply deep domain knowledge: software architecture patterns, dependency "
        "management, API design, deployment and operational constraints, and "
        "backward compatibility requirements."
    ),
)

RESEARCH_PROFILE = DomainProfile(
    name="research",
    risk_vocabulary=[
        "methodological rigor",
        "reproducibility",
        "statistical validity",
        "domain coverage",
        "publication bias",
    ],
    domain_constraints=[
        "peer review standards",
        "reproducibility requirements",
        "ethical considerations",
    ],
    specialist_focus=(
        "Apply deep domain knowledge: research methodology, statistical rigor, "
        "reproducibility standards, peer review requirements, and domain-specific "
        "best practices."
    ),
)

DEFAULT_PROFILE = DomainProfile(
    name="default",
    risk_vocabulary=[
        "feasibility risk",
        "scope risk",
        "resource risk",
        "integration risk",
        "quality risk",
    ],
    domain_constraints=[
        "resource constraints",
        "timeline constraints",
        "integration requirements",
    ],
    specialist_focus=(
        "Apply deep domain knowledge: feasibility analysis, trade-off assessment, "
        "assumption validation, evidence quality, and real-world operational "
        "constraints."
    ),
)

_BUILT_IN_PROFILES: dict[str, DomainProfile] = {
    "finance": FINANCE_PROFILE,
    "software": SOFTWARE_PROFILE,
    "research": RESEARCH_PROFILE,
    "default": DEFAULT_PROFILE,
}


def resolve_profile(
    profile_or_name: DomainProfile | str | None = None,
) -> DomainProfile:
    """Resolve a profile name / object / absent to a :class:`DomainProfile`.

    Port of TS ``resolveProfile``: a falsy argument (``None`` / ``""``) returns
    :data:`DEFAULT_PROFILE`; a string looks up the built-in (unknown names fall
    back to :data:`DEFAULT_PROFILE` via the ``?? DEFAULT_PROFILE`` coalesce); a
    :class:`DomainProfile` passes through unchanged.
    """
    if not profile_or_name:
        return DEFAULT_PROFILE
    if isinstance(profile_or_name, str):
        return _BUILT_IN_PROFILES.get(profile_or_name, DEFAULT_PROFILE)
    return profile_or_name


# ── Constants ─────────────────────────────────────────────────────────

_VALID_INPUT_TYPES: list[DebateInputType] = ["knowledge-overview", "design-spec"]

_ROUND_1_ROLES: list[AgentRole] = [
    "advocate",
    "critic",
    "risk_specialist",
    "domain_specialist",
]

_ALL_ROLES: list[AgentRole] = [*_ROUND_1_ROLES, "synthesizer"]


# ── init_debate ───────────────────────────────────────────────────────


def init_debate(
    input_type: DebateInputType,
    input_id: str,
    input_version: str,
    artifact_content: object,
    artifact_path: str | None = None,
    codebase_requirements_id: str | None = None,
    domain_profile: DomainProfile | str | None = None,
) -> DebateState:
    """Initialise a fresh :class:`DebateState` (port of TS ``initDebate``).

    Raises :class:`ValueError` with the byte-exact Node message when
    ``input_type`` is not one of the two valid types.
    """
    if input_type not in _VALID_INPUT_TYPES:
        raise ValueError(
            f'Invalid input_type: "{input_type}". '
            f"Must be one of: {', '.join(_VALID_INPUT_TYPES)}"
        )

    return DebateState(
        transcript_id=f"dt-{secrets.token_hex(4)}",
        input_type=input_type,
        input_id=input_id,
        input_version=input_version,
        output_version=None,
        created_date=_now_iso(),
        artifact_content=artifact_content,
        artifact_path=artifact_path,
        round1_arguments=[],
        synthesis=None,
        codebase_requirements_id=codebase_requirements_id,
        kb_queries_used=False,
        domain_profile=resolve_profile(domain_profile),
    )


# ── submit_argument ───────────────────────────────────────────────────


def submit_argument(state: DebateState, arg: AgentArgument) -> DebateState:
    """Append a validated round-1 argument, returning a new state.

    Port of TS ``submitArgument``. Raises :class:`ValueError` with byte-exact
    Node messages for an unknown role, a synthesizer role, or an out-of-range
    confidence. The three optional arrays coalesce to ``[]`` only when ``None``
    (the TS ``?? []``) — an explicit empty list or populated list survives.
    """
    if arg.role not in _ALL_ROLES:
        raise ValueError(
            f'Invalid role: "{arg.role}". Must be one of: {", ".join(_ALL_ROLES)}'
        )

    if arg.role == "synthesizer":
        raise ValueError(
            "Synthesizer arguments are recorded via pipeline_synthesize_debate, "
            "not pipeline_submit_argument."
        )

    if arg.confidence < 0 or arg.confidence > 1:
        raise ValueError("confidence must be between 0 and 1")

    appended = AgentArgument(
        role=arg.role,
        position=arg.position,
        evidence=[] if arg.evidence is None else arg.evidence,
        counterpoints=[] if arg.counterpoints is None else arg.counterpoints,
        proposed_changes=(
            [] if arg.proposed_changes is None else arg.proposed_changes
        ),
        confidence=arg.confidence,
    )

    return DebateState(
        transcript_id=state.transcript_id,
        input_type=state.input_type,
        input_id=state.input_id,
        input_version=state.input_version,
        output_version=state.output_version,
        created_date=state.created_date,
        artifact_content=state.artifact_content,
        artifact_path=state.artifact_path,
        round1_arguments=[*state.round1_arguments, appended],
        synthesis=state.synthesis,
        codebase_requirements_id=state.codebase_requirements_id,
        kb_queries_used=state.kb_queries_used,
        domain_profile=state.domain_profile,
    )


# ── build_transcript ──────────────────────────────────────────────────


def build_transcript(state: DebateState) -> dict[str, object]:
    """Assemble the persisted debate transcript dict (port of TS ``buildTranscript``).

    Raises :class:`ValueError` (byte-exact Node message) when ``synthesis`` has
    not yet been recorded. Returns a plain ``dict`` whose key insertion order
    reproduces the TS object literal + conditional appends so the on-disk
    ``json.dumps(transcript, indent=2, ensure_ascii=False)`` matches Node's
    ``JSON.stringify(transcript, null, 2)`` byte-for-byte.
    """
    if state.synthesis is None:
        raise ValueError(
            "synthesis not yet recorded — call pipeline_synthesize_debate first"
        )

    round1_agents: list[dict[str, object]] = [
        {
            "role": arg.role,
            "position": arg.position,
            "evidence": arg.evidence,
            "counterpoints": arg.counterpoints,
            "proposed_changes": arg.proposed_changes,
            "confidence": arg.confidence,
        }
        for arg in state.round1_arguments
    ]

    synthesis = state.synthesis
    open_questions = synthesis.open_questions
    synthesizer_position_lines = [
        "Accepted changes: "
        + ("; ".join(synthesis.changes_accepted) or "none"),
        "Rejected changes: "
        + ("; ".join(r["proposed"] for r in synthesis.changes_rejected) or "none"),
    ]
    if open_questions:
        synthesizer_position_lines.append(
            "Open questions: " + "; ".join(open_questions)
        )

    round2_agents: list[dict[str, object]] = [
        {
            "role": "synthesizer",
            "position": ". ".join(synthesizer_position_lines),
            "evidence": [],
            "counterpoints": [],
            "proposed_changes": [],
            "confidence": 1,
        }
    ]

    # Mirror the Node object literal + ``JSON.stringify`` undefined-drop: Node
    # sets ``open_questions: state.synthesis.open_questions`` and serialisation
    # omits the key entirely when the value is ``undefined`` (Python ``None``).
    # Inserting it conditionally keeps the key order (accepted, rejected, then
    # open_questions when present) and avoids emitting ``"open_questions": null``,
    # which ``debate-transcript.json`` rejects (it types the field as ``array``).
    synthesis_block: dict[str, object] = {
        "changes_accepted": synthesis.changes_accepted,
        "changes_rejected": synthesis.changes_rejected,
    }
    if synthesis.open_questions is not None:
        synthesis_block["open_questions"] = synthesis.open_questions

    transcript: dict[str, object] = {
        "transcript_id": state.transcript_id,
        "input_type": state.input_type,
        "input_id": state.input_id,
        "created_date": state.created_date,
        "kb_queries_used": state.kb_queries_used,
        "rounds": [
            {"round": 1, "type": "divergent", "agents": round1_agents},
            {"round": 2, "type": "convergent", "agents": round2_agents},
        ],
        "synthesis": synthesis_block,
    }

    if state.input_version:
        transcript["input_version"] = state.input_version
    if state.output_version:
        transcript["output_version"] = state.output_version
    if state.codebase_requirements_id:
        transcript["codebase_requirements_id"] = state.codebase_requirements_id
    if state.domain_profile.name != "default":
        transcript["domain_profile"] = state.domain_profile.name

    return transcript


# ── generate_agent_prompts ────────────────────────────────────────────


def generate_agent_prompts(state: DebateState) -> list[AgentPrompt]:
    """Build the four round-1 agent prompts (port of TS ``generateAgentPrompts``).

    The risk-specialist prompt interpolates ``profile.risk_vocabulary`` and the
    domain-specialist prompt interpolates ``profile.specialist_focus`` — exactly
    the TS template literals, byte-for-byte.
    """
    input_label = (
        "knowledge overview"
        if state.input_type == "knowledge-overview"
        else "design spec"
    )
    profile = state.domain_profile

    role_instructions: dict[str, str] = {
        "advocate": (
            "Your role is ADVOCATE. Defend the current artifact's decisions with "
            "evidence from the source material and domain knowledge. Identify what "
            "is strong, well-reasoned, and supported. Propose only changes that "
            "strengthen the existing approach rather than redirecting it."
        ),
        "critic": (
            "Your role is CRITIC. Challenge the artifact's assumptions, identify "
            "weaknesses, gaps in reasoning, and under-explored alternatives. Your "
            "goal is to find what is missing, overstated, or fragile. Propose "
            "concrete alternative approaches or changes where the current direction "
            "is flawed."
        ),
        "risk_specialist": (
            "Your role is RISK SPECIALIST. Evaluate risk models, failure modes, "
            "worst-case scenarios, and the adequacy of mitigation strategies in "
            f"this artifact. Assess {', '.join(profile.risk_vocabulary)}. Identify "
            "specific gaps in the artifact's risk treatment and propose concrete "
            "mitigations."
        ),
        "domain_specialist": (
            f"Your role is DOMAIN SPECIALIST. {profile.specialist_focus} Identify "
            "where the artifact makes assumptions that conflict with real-world "
            "constraints or misses domain-specific best practices."
        ),
    }

    prompts: list[AgentPrompt] = []
    for role in _ROUND_1_ROLES:
        instruction = role_instructions[role]
        prompt = "\n".join(
            [
                f"You are participating in an adversarial debate reviewing a {input_label}.",  # noqa: E501
                f"Artifact ID: {state.input_id} (version {state.input_version})",
                "",
                instruction,
                "",
                "After your analysis, respond with a JSON object matching this structure:",  # noqa: E501
                "{",
                '  "position": "your primary argument or assessment (1-3 sentences)",',
                '  "evidence": ["source_id or reference supporting your position"],',
                '  "counterpoints": ["specific counterarguments to the artifact or anticipated opposing views"],',  # noqa: E501
                '  "proposed_changes": ["concrete, actionable change you recommend to the artifact"],',  # noqa: E501
                '  "confidence": 0.0-1.0',
                "}",
            ]
        )
        prompts.append(AgentPrompt(role=role, prompt=prompt))

    return prompts


# ── build_artifact_structure_summary ──────────────────────────────────


def build_artifact_structure_summary(content: object) -> str:
    """Summarise an artifact's top-level shape without embedding its content.

    Port of TS ``buildArtifactStructureSummary``. For a true non-object
    (``None``, str, number, bool) returns ``"(non-object artifact, <typeof>)"``
    using the JS ``typeof`` label; a dict **or** a top-level list (Node:
    ``typeof [] === \'object\'``, so arrays fall through ``Object.entries`` to
    string index keys) emits one ``  - key: ...`` line per top-level entry,
    summarising strings (truncated at 60 chars), arrays (item count), nested
    objects (key count), and primitives (stringified).
    """
    # Node guards with ``typeof content !== \'object\'``; since
    # ``typeof [] === \'object\'``, a top-level array falls through to
    # ``Object.entries``, which yields string index keys ("0", "1", ...). Mirror
    # that by iterating a ``list`` as ``(str(index), value)`` pairs. Only true
    # non-objects (``None``, str, number, bool) hit the fallback.
    if content is None or not isinstance(content, (dict, list)):
        return f"(non-object artifact, {_js_typeof(content)})"

    if isinstance(content, list):
        entries: list[tuple[str, object]] = [
            (str(index), value) for index, value in enumerate(content)
        ]
    else:
        entries = list(content.items())

    lines: list[str] = []
    for key, value in entries:
        if isinstance(value, str):
            if len(value) > 60:
                lines.append(f'  - {key}: "{value[:60]}..."')
            else:
                lines.append(f'  - {key}: "{value}"')
        elif isinstance(value, list):
            lines.append(f"  - {key}: {len(value)} items")
        elif value is not None and isinstance(value, dict):
            lines.append(f"  - {key}: {{{len(value)} keys}}")
        else:
            lines.append(f"  - {key}: {_js_string(value)}")
    return "\n".join(lines)


def _js_typeof(value: object) -> str:
    """Mirror the JS ``typeof`` label for the subset used by the summary.

    ``None`` → ``"object"`` (JS ``typeof null === 'object'``), ``bool`` /
    ``int`` / ``float`` → ``"number"`` (booleans are numbers only loosely in JS,
    but the summary only reaches this for non-object scalars passed as the whole
    artifact — strings → ``"string"``, everything else → ``"object"``). The
    early ``content is None or not isinstance(content, dict)`` branch is the only
    caller, so this is reached for ``None`` (→ ``"object"``), strings (→
    ``"string"``), numbers (→ ``"number"``), and booleans (→ ``"boolean"``).
    """
    if value is None:
        return "object"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    return "object"


def _js_string(value: object) -> str:
    """Coerce ``value`` the way JS ``String(value)`` does (subset used here).

    ``True``→``"true"``, ``False``→``"false"``, ``None``→``"null"``, whole
    floats render without ``.0`` (``2.0``→``"2"``).
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return repr(value)
    return str(value)


# ── generate_synthesis_prompt ─────────────────────────────────────────


def generate_synthesis_prompt(state: DebateState) -> str:
    """Build the synthesizer agent prompt (port of TS ``generateSynthesisPrompt``).

    Raises :class:`ValueError` (byte-exact Node message) when no round-1
    arguments have been submitted. Emits the argument sections, an optional
    domain note (non-default profiles only), an optional artifact-path reference,
    and the artifact structure summary — never the full artifact JSON.
    """
    if len(state.round1_arguments) == 0:
        raise ValueError(
            "No round-1 arguments submitted yet — run all 4 debate agents first"
        )

    input_label = (
        "knowledge overview"
        if state.input_type == "knowledge-overview"
        else "design spec"
    )

    argument_blocks: list[str] = []
    for arg in state.round1_arguments:
        block_lines = [
            f"### {arg.role.upper()} (confidence: {_js_string(arg.confidence)})",
            f"**Position:** {arg.position}",
        ]
        if arg.evidence:
            block_lines.append(f"**Evidence:** {', '.join(arg.evidence)}")
        if arg.counterpoints:
            block_lines.append(f"**Counterpoints:** {'; '.join(arg.counterpoints)}")
        if arg.proposed_changes:
            block_lines.append(
                f"**Proposed changes:** {'; '.join(arg.proposed_changes)}"
            )
        argument_blocks.append("\n".join(block_lines))
    argument_sections = "\n\n".join(argument_blocks)

    profile = state.domain_profile
    domain_note = (
        f" Domain: {profile.name}. "
        f"Key constraints: {', '.join(profile.domain_constraints)}."
        if profile.name != "default"
        else ""
    )

    artifact_ref_lines = (
        [f"Path: {state.artifact_path} (read this file for full content)"]
        if state.artifact_path is not None
        else []
    )

    lines = [
        f"You are SYNTHESIZER in an adversarial debate review of a {input_label}.{domain_note}",  # noqa: E501
        "",
        "Your task: evaluate all round-1 arguments, resolve conflicts, and produce a final synthesis decision.",  # noqa: E501
        "",
        "## Round 1 Arguments",
        "",
        argument_sections,
        "",
        "## Artifact Reference",
        "",
        *artifact_ref_lines,
        f"Artifact ID: {state.input_id} (version {state.input_version})",
        "",
        "## Artifact Structure (key summary)",
        "",
        build_artifact_structure_summary(state.artifact_content),
        "",
        "Respond with a JSON object matching this structure:",
        "{",
        '  "changes_accepted": ["change description — rationale"],',
        '  "changes_rejected": [{ "proposed": "change text", "reason": "why rejected" }],',  # noqa: E501
        '  "open_questions": ["unresolved question requiring further research or human decision"]',  # noqa: E501
        "}",
        "",
        "Be rigorous: accept changes that are well-evidenced and improve the artifact. Reject changes that are speculative, contradicted by evidence, or outside scope. Flag genuinely unresolved issues as open questions.",  # noqa: E501
    ]

    return "\n".join(lines)


# ── Per-run debate write helpers (Feature C) ──────────────────────────


def resolve_debates_dir(
    run_data_dir: str | None,
    legacy_base_dir: str,
) -> str:
    """Resolve the on-disk directory for debate transcripts (TS ``resolveDebatesDir``).

    ``get_artifact_dir(run_data_dir, 'debates')`` when ``run_data_dir`` is truthy,
    else ``{legacy_base_dir}/debates``. An empty-string ``run_data_dir`` is falsy
    and falls back.
    """
    if run_data_dir:
        return get_artifact_dir(run_data_dir, "debates")
    return os.path.join(legacy_base_dir, "debates")


def persist_debate(
    run_data_dir: str | None,
    legacy_base_dir: str,
    transcript: object,
    file_name: str,
    force: bool | None = None,
) -> str:
    """Persist a debate transcript under the run's data dir (TS ``persistDebate``).

    Unlike ``persist_raw_collection`` / ``persist_overview`` (overwrite-ok), debate
    transcripts are immutable once saved: the target is created if absent, but a
    pre-existing file raises :class:`FileExistsError` (byte-exact Node message)
    unless ``force is True``. Returns the absolute path written.
    """
    directory = resolve_debates_dir(run_data_dir, legacy_base_dir)
    os.makedirs(directory, exist_ok=True)
    file_path = os.path.join(directory, file_name)
    if os.path.exists(file_path) and force is not True:
        raise FileExistsError(
            f'Debate transcript already exists at "{file_path}". '
            "Pass force=true to overwrite."
        )
    with open(file_path, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(transcript, indent=2, ensure_ascii=False))
    return file_path
