"""Port of ``legacy-node/tests/debate.test.ts`` (60 cases).

Exercises the debate engine 1:1 with the Node suite: ``init_debate`` /
``submit_argument`` / ``build_transcript`` / ``generate_agent_prompts`` /
``generate_synthesis_prompt`` / ``resolve_profile`` / the four built-in profiles
/ ``init_debate`` with a domain profile / ``handle_init_debate`` (the
artifact-once pattern) / the Feature-C ``resolve_debates_dir`` /
``persist_debate`` write helpers — preserving every assertion.

The TS suite mutated ``state.synthesis = {...}`` / ``state.outputVersion =
'1.1.0'`` directly on the returned object; the Python port assigns the
:class:`DebateSynthesis` / ``output_version`` attributes on the dataclass, which
is mutable by design (mirroring the Node object-literal mutation).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

import pytest

from pipeline_orchestrator.debate import (
    DEFAULT_PROFILE,
    FINANCE_PROFILE,
    RESEARCH_PROFILE,
    SOFTWARE_PROFILE,
    build_artifact_structure_summary,
    build_transcript,
    generate_agent_prompts,
    generate_synthesis_prompt,
    init_debate,
    persist_debate,
    resolve_debates_dir,
    resolve_profile,
    submit_argument,
)
from pipeline_orchestrator.models import (
    AgentArgument,
    DebateState,
    DebateSynthesis,
    DomainProfile,
)
from pipeline_orchestrator.tools.debate_tools import (
    DebateContext,
    handle_init_debate,
)
from pipeline_orchestrator.validator import load_schemas, validate_artifact

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"

# ── init_debate ───────────────────────────────────────────────────────


def test_init_debate_knowledge_overview() -> None:
    state = init_debate(
        input_type="knowledge-overview",
        input_id="ov-abc12345",
        input_version="1.0.0",
        artifact_content={
            "overview_id": "ov-abc12345",
            "title": "Kalshi Edge Strategies",
        },
    )
    assert state.input_type == "knowledge-overview"
    assert state.input_id == "ov-abc12345"
    assert state.input_version == "1.0.0"
    assert state.transcript_id.startswith("dt-")
    assert len(state.transcript_id) == 11  # dt- + 8 hex chars
    assert state.round1_arguments == []
    assert state.synthesis is None
    assert state.created_date


def test_init_debate_design_spec() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-def67890",
        input_version="2.1.0",
        artifact_content={
            "spec_id": "spec-def67890",
            "title": "Kalshi Market Maker",
        },
    )
    assert state.input_type == "design-spec"
    assert state.input_id == "spec-def67890"


def test_init_debate_throws_on_invalid_input_type() -> None:
    with pytest.raises(ValueError, match="(?i)invalid input_type"):
        init_debate(
            input_type="raw-collection",  # type: ignore[arg-type]
            input_id="x",
            input_version="1.0.0",
            artifact_content={},
        )


def test_init_debate_assigns_unique_transcript_id_each_call() -> None:
    s1 = init_debate(
        input_type="knowledge-overview",
        input_id="a",
        input_version="1.0.0",
        artifact_content={},
    )
    s2 = init_debate(
        input_type="knowledge-overview",
        input_id="a",
        input_version="1.0.0",
        artifact_content={},
    )
    assert s1.transcript_id != s2.transcript_id


# ── submit_argument ───────────────────────────────────────────────────


def _make_state() -> DebateState:
    return init_debate(
        input_type="design-spec",
        input_id="spec-test",
        input_version="1.0.0",
        artifact_content={"spec_id": "spec-test", "title": "Test"},
    )


def test_submit_argument_records_advocate() -> None:
    state = submit_argument(
        _make_state(),
        AgentArgument(
            role="advocate",
            position="The strategy has strong empirical backing from 6 months of data.",
            evidence=["item-001", "item-002"],
            counterpoints=[],
            proposed_changes=[],
            confidence=0.9,
        ),
    )
    assert len(state.round1_arguments) == 1
    assert state.round1_arguments[0].role == "advocate"
    assert state.round1_arguments[0].confidence == 0.9


def test_submit_argument_records_multiple_from_different_agents() -> None:
    state = _make_state()
    state = submit_argument(
        state,
        AgentArgument(
            role="advocate", position="Strong fundamentals.", confidence=0.85
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(role="critic", position="Overfitting risk.", confidence=0.7),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="risk_specialist",
            position="Drawdown inadequately modelled.",
            confidence=0.8,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="domain_specialist",
            position="Missing regulatory constraints.",
            confidence=0.75,
        ),
    )
    assert len(state.round1_arguments) == 4


def test_submit_argument_throws_on_unknown_role() -> None:
    with pytest.raises(ValueError, match="(?i)invalid role"):
        submit_argument(
            _make_state(),
            AgentArgument(role="unknown_agent", position="x", confidence=0.5),  # type: ignore[arg-type]
        )


def test_submit_argument_throws_if_synthesis_role_in_round_1() -> None:
    with pytest.raises(
        ValueError, match="(?is)synthesizer.*pipeline_synthesize_debate"
    ):
        submit_argument(
            _make_state(),
            AgentArgument(role="synthesizer", position="x", confidence=0.5),
        )


def test_submit_argument_throws_if_confidence_out_of_range() -> None:
    with pytest.raises(ValueError, match="confidence must be between 0 and 1"):
        submit_argument(
            _make_state(),
            AgentArgument(role="critic", position="x", confidence=1.5),
        )


# ── build_transcript ──────────────────────────────────────────────────


def _make_full_state() -> DebateState:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-full",
        input_version="1.0.0",
        artifact_content={"spec_id": "spec-full", "title": "Full"},
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="advocate",
            position="Solid approach.",
            evidence=["src-001"],
            counterpoints=[],
            proposed_changes=[],
            confidence=0.9,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="critic",
            position="Execution risk is understated.",
            evidence=[],
            counterpoints=["Slippage not modelled"],
            proposed_changes=["Add slippage model"],
            confidence=0.7,
        ),
    )
    state.synthesis = DebateSynthesis(
        changes_accepted=["Add slippage model"],
        changes_rejected=[],
        open_questions=["What is realistic slippage for Kalshi limits?"],
    )
    state.output_version = "1.1.0"
    return state


def test_build_transcript_structurally_complete() -> None:
    transcript = build_transcript(_make_full_state())
    assert isinstance(transcript["transcript_id"], str)
    assert transcript["transcript_id"].startswith("dt-")
    assert transcript["input_type"] == "design-spec"
    assert transcript["input_id"] == "spec-full"
    assert transcript["input_version"] == "1.0.0"
    assert transcript["output_version"] == "1.1.0"
    rounds = transcript["rounds"]
    assert isinstance(rounds, list)
    assert len(rounds) == 2  # round 1 divergent + round 2 convergent
    assert rounds[0]["type"] == "divergent"
    assert rounds[1]["type"] == "convergent"
    assert len(rounds[0]["agents"]) == 2  # advocate + critic
    assert len(rounds[1]["agents"]) == 1  # synthesizer
    synthesis = transcript["synthesis"]
    assert isinstance(synthesis, dict)
    assert "Add slippage model" in synthesis["changes_accepted"]


def test_build_transcript_throws_if_synthesis_not_recorded() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-incomplete",
        input_version="1.0.0",
        artifact_content={},
    )
    with pytest.raises(ValueError, match="(?i)synthesis not yet recorded"):
        build_transcript(state)


def _make_state_with_synthesis(
    open_questions: list[str] | None,
) -> DebateState:
    """Full debate state with a synthesis whose ``open_questions`` is set/unset."""
    state = init_debate(
        input_type="design-spec",
        input_id="spec-oq",
        input_version="1.0.0",
        artifact_content={"spec_id": "spec-oq", "title": "OQ"},
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="advocate",
            position="Solid approach.",
            evidence=["src-001"],
            counterpoints=[],
            proposed_changes=[],
            confidence=0.9,
        ),
    )
    state.synthesis = DebateSynthesis(
        changes_accepted=["Add slippage model"],
        changes_rejected=[],
        open_questions=open_questions,
    )
    return state


def test_build_transcript_omits_open_questions_key_when_none() -> None:
    """Node sets ``open_questions: undefined`` → ``JSON.stringify`` drops the key.

    When ``DebateSynthesis.open_questions is None`` the serialised synthesis
    subobject must have NO ``open_questions`` key (not ``null``), and the
    resulting transcript must PASS validation against ``debate-transcript.json``
    (where ``null`` would be ``not of type 'array'``).
    """
    state = _make_state_with_synthesis(None)
    transcript = build_transcript(state)

    synthesis = transcript["synthesis"]
    assert isinstance(synthesis, dict)
    assert "open_questions" not in synthesis
    # Key order matches Node's object literal: accepted, then rejected.
    assert list(synthesis.keys()) == ["changes_accepted", "changes_rejected"]

    # Round-trip through JSON so the assertion pins the serialised bytes, not
    # just the in-memory dict (mirrors Node ``JSON.stringify``).
    serialized = json.loads(json.dumps(transcript))
    assert "open_questions" not in serialized["synthesis"]

    schemas = load_schemas(SCHEMAS_DIR)
    result = validate_artifact(schemas, "debate-transcript.json", serialized)
    assert result.valid is True, result.errors


def test_build_transcript_emits_open_questions_key_when_present() -> None:
    """A populated ``open_questions`` is still emitted (as an array) and valid."""
    state = _make_state_with_synthesis(
        ["What is realistic slippage for Kalshi limits?"]
    )
    transcript = build_transcript(state)

    synthesis = transcript["synthesis"]
    assert isinstance(synthesis, dict)
    assert synthesis["open_questions"] == [
        "What is realistic slippage for Kalshi limits?"
    ]
    assert list(synthesis.keys()) == [
        "changes_accepted",
        "changes_rejected",
        "open_questions",
    ]

    serialized = json.loads(json.dumps(transcript))
    schemas = load_schemas(SCHEMAS_DIR)
    result = validate_artifact(schemas, "debate-transcript.json", serialized)
    assert result.valid is True, result.errors


# ── build_artifact_structure_summary ──────────────────────────────────


def test_build_artifact_structure_summary_top_level_array() -> None:
    """Node: ``typeof [] === 'object'`` → arrays fall through ``Object.entries``.

    A top-level list is summarised with string index keys (not the non-object
    fallback), matching the live Node oracle byte-for-byte.
    """
    content = ["a", {"x": 1}, [1, 2, 3], 5, True, None, "x" * 70]
    summary = build_artifact_structure_summary(content)
    expected = (
        '  - 0: "a"\n'
        "  - 1: {1 keys}\n"
        "  - 2: 3 items\n"
        "  - 3: 5\n"
        "  - 4: true\n"
        "  - 5: null\n"
        '  - 6: "' + ("x" * 60) + '..."'
    )
    assert summary == expected


def test_build_artifact_structure_summary_non_object_still_fallback() -> None:
    """Genuine non-objects (str/number/bool/None) still hit the fallback."""
    assert (
        build_artifact_structure_summary("hi")
        == "(non-object artifact, string)"
    )
    assert build_artifact_structure_summary(5) == "(non-object artifact, number)"
    assert (
        build_artifact_structure_summary(True)
        == "(non-object artifact, boolean)"
    )
    assert (
        build_artifact_structure_summary(None)
        == "(non-object artifact, object)"
    )


# ── generate_agent_prompts ────────────────────────────────────────────


def test_generate_agent_prompts_returns_all_4_round1_agents() -> None:
    state = init_debate(
        input_type="knowledge-overview",
        input_id="ov-test",
        input_version="1.0.0",
        artifact_content={"overview_id": "ov-test", "title": "Test Overview"},
    )
    prompts = generate_agent_prompts(state)
    assert len(prompts) == 4
    roles = [p.role for p in prompts]
    assert "advocate" in roles
    assert "critic" in roles
    assert "risk_specialist" in roles
    assert "domain_specialist" in roles


def test_generate_agent_prompts_each_contains_artifact_id() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-prompt-test",
        input_version="1.0.0",
        artifact_content={"spec_id": "spec-prompt-test", "strategy_name": "Event Arb"},
    )
    prompts = generate_agent_prompts(state)
    for p in prompts:
        assert "spec-prompt-test" in p.prompt, f"{p.role} prompt missing artifact ID"


def test_generate_agent_prompts_do_not_contain_full_artifact_json() -> None:
    artifact_content = {
        "spec_id": "spec-no-embed",
        "strategy_name": "Event Arb",
        "unique_marker": "SHOULD_NOT_APPEAR_IN_PROMPT",
    }
    state = init_debate(
        input_type="design-spec",
        input_id="spec-no-embed",
        input_version="1.0.0",
        artifact_content=artifact_content,
    )
    prompts = generate_agent_prompts(state)
    artifact_json = json.dumps(artifact_content, indent=2)
    for p in prompts:
        assert "unique_marker" not in p.prompt
        assert "SHOULD_NOT_APPEAR_IN_PROMPT" not in p.prompt
        assert artifact_json not in p.prompt


def test_generate_agent_prompts_critic_emphasises_weaknesses() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-critic",
        input_version="1.0.0",
        artifact_content={},
    )
    prompts = generate_agent_prompts(state)
    critic_prompt = next(p for p in prompts if p.role == "critic")
    assert (
        "weakn" in critic_prompt.prompt.lower()
        or "challeng" in critic_prompt.prompt.lower()
    )


def test_generate_agent_prompts_risk_interpolates_risk_vocabulary() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-finance",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="finance",
    )
    prompts = generate_agent_prompts(state)
    risk_prompt = next(p for p in prompts if p.role == "risk_specialist")
    assert "liquidity risk" in risk_prompt.prompt
    assert "execution risk" in risk_prompt.prompt


def test_generate_agent_prompts_domain_interpolates_specialist_focus() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-finance",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="finance",
    )
    prompts = generate_agent_prompts(state)
    domain_prompt = next(p for p in prompts if p.role == "domain_specialist")
    assert "market microstructure" in domain_prompt.prompt


def test_finance_profile_preserves_finance_terms_in_prompts() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-finance-eq",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="finance",
    )
    prompts = generate_agent_prompts(state)
    risk_prompt = next(p for p in prompts if p.role == "risk_specialist")
    domain_prompt = next(p for p in prompts if p.role == "domain_specialist")
    assert "liquidity risk" in risk_prompt.prompt
    assert "operational risk" in risk_prompt.prompt
    assert "tail risk" in risk_prompt.prompt
    assert "market microstructure" in domain_prompt.prompt
    assert "settlement timing" in domain_prompt.prompt
    assert "regulatory requirements" in domain_prompt.prompt


def test_software_profile_produces_software_prompts() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-software",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="software",
    )
    prompts = generate_agent_prompts(state)
    risk_prompt = next(p for p in prompts if p.role == "risk_specialist")
    domain_prompt = next(p for p in prompts if p.role == "domain_specialist")
    assert "technical debt" in risk_prompt.prompt
    assert "API stability" in risk_prompt.prompt
    assert "software architecture" in domain_prompt.prompt
    assert "liquidity risk" not in risk_prompt.prompt
    assert "market microstructure" not in domain_prompt.prompt


def test_research_profile_produces_research_prompts() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-research",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="research",
    )
    prompts = generate_agent_prompts(state)
    risk_prompt = next(p for p in prompts if p.role == "risk_specialist")
    domain_prompt = next(p for p in prompts if p.role == "domain_specialist")
    assert "reproducibility" in risk_prompt.prompt
    assert "statistical validity" in risk_prompt.prompt
    assert "research methodology" in domain_prompt.prompt


def test_default_profile_uses_generic_vocabulary() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-generic",
        input_version="1.0.0",
        artifact_content={},
    )
    prompts = generate_agent_prompts(state)
    risk_prompt = next(p for p in prompts if p.role == "risk_specialist")
    domain_prompt = next(p for p in prompts if p.role == "domain_specialist")
    assert "feasibility risk" in risk_prompt.prompt
    assert "liquidity risk" not in risk_prompt.prompt
    assert "market microstructure" not in risk_prompt.prompt
    assert (
        "feasibility analysis" in domain_prompt.prompt
        or "trade-off" in domain_prompt.prompt
    )


def test_custom_domain_profile_object_interpolates() -> None:
    custom = DomainProfile(
        name="aerospace",
        risk_vocabulary=["mission-critical failure", "thermal risk"],
        domain_constraints=["FAA regulations"],
        specialist_focus="Apply aerospace engineering expertise.",
    )
    state = init_debate(
        input_type="design-spec",
        input_id="spec-aero",
        input_version="1.0.0",
        artifact_content={},
        domain_profile=custom,
    )
    prompts = generate_agent_prompts(state)
    risk_prompt = next(p for p in prompts if p.role == "risk_specialist")
    domain_prompt = next(p for p in prompts if p.role == "domain_specialist")
    assert "mission-critical failure" in risk_prompt.prompt
    assert "thermal risk" in risk_prompt.prompt
    assert "aerospace engineering" in domain_prompt.prompt


# ── generate_synthesis_prompt ─────────────────────────────────────────


def test_synthesis_prompt_includes_all_round1_arguments() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-synth",
        input_version="1.0.0",
        artifact_content={"title": "Synth Test"},
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="advocate", position="Strong data support.", confidence=0.85
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(role="critic", position="Parameter instability.", confidence=0.7),
    )
    prompt = generate_synthesis_prompt(state)
    assert "Strong data support." in prompt
    assert "Parameter instability." in prompt
    assert "synthes" in prompt.lower()


def test_synthesis_prompt_throws_if_no_round1_arguments() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-empty",
        input_version="1.0.0",
        artifact_content={},
    )
    with pytest.raises(ValueError, match="(?i)no round.1 arguments"):
        generate_synthesis_prompt(state)


def test_synthesis_prompt_includes_domain_name_and_constraints() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-domain-synth",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="finance",
    )
    state = submit_argument(
        state,
        AgentArgument(role="advocate", position="x", confidence=0.5),
    )
    prompt = generate_synthesis_prompt(state)
    assert "Domain: finance" in prompt
    assert "market microstructure" in prompt


def test_synthesis_prompt_omits_domain_note_for_default() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-default-synth",
        input_version="1.0.0",
        artifact_content={},
    )
    state = submit_argument(
        state,
        AgentArgument(role="advocate", position="x", confidence=0.5),
    )
    prompt = generate_synthesis_prompt(state)
    assert "Domain: default" not in prompt
    assert "Key constraints" not in prompt


def test_synthesis_prompt_does_not_embed_full_artifact_json() -> None:
    big_array = [
        {
            "id": f"component-{i}",
            "weight": i * 0.05,
            "config": {"mode": "active", "priority": i},
        }
        for i in range(20)
    ]
    artifact_content = {
        "spec_id": "spec-synth-no-embed",
        "strategy_name": "Market Maker Strategy with Chunked Components",
        "components": big_array,
        "execution_rules": {
            "max_position": 1000,
            "slippage_model": "linear",
            "timeout_ms": 5000,
        },
    }
    state = init_debate(
        input_type="design-spec",
        input_id="spec-synth-no-embed",
        input_version="1.0.0",
        artifact_content=artifact_content,
        artifact_path="/tmp/artifacts/spec-synth-no-embed.json",
    )
    state = submit_argument(
        state,
        AgentArgument(role="advocate", position="Well tested.", confidence=0.8),
    )
    state = submit_argument(
        state,
        AgentArgument(role="critic", position="Lacks edge cases.", confidence=0.65),
    )
    prompt = generate_synthesis_prompt(state)
    full_json = json.dumps(artifact_content, indent=2)
    assert full_json not in prompt
    assert '"component-0"' not in prompt
    assert '"component-19"' not in prompt
    assert "20 items" in prompt
    assert "/tmp/artifacts/spec-synth-no-embed.json" in prompt
    assert "spec-synth-no-embed" in prompt


# ── resolve_profile ───────────────────────────────────────────────────


def test_resolve_profile_default_when_no_argument() -> None:
    profile = resolve_profile()
    assert profile.name == "default"
    assert profile is DEFAULT_PROFILE


def test_resolve_profile_default_when_argument_none() -> None:
    profile = resolve_profile(None)
    assert profile.name == "default"


def test_resolve_profile_finance_string() -> None:
    profile = resolve_profile("finance")
    assert profile.name == "finance"
    assert profile is FINANCE_PROFILE


def test_resolve_profile_software_string() -> None:
    profile = resolve_profile("software")
    assert profile.name == "software"
    assert profile is SOFTWARE_PROFILE


def test_resolve_profile_research_string() -> None:
    profile = resolve_profile("research")
    assert profile.name == "research"
    assert profile is RESEARCH_PROFILE


def test_resolve_profile_default_string() -> None:
    profile = resolve_profile("default")
    assert profile.name == "default"
    assert profile is DEFAULT_PROFILE


def test_resolve_profile_unknown_name_falls_back_to_default() -> None:
    profile = resolve_profile("made-up-domain")
    assert profile.name == "default"
    assert profile is DEFAULT_PROFILE


def test_resolve_profile_passes_object_through() -> None:
    custom = DomainProfile(
        name="healthcare",
        risk_vocabulary=["clinical risk", "regulatory compliance"],
        domain_constraints=["HIPAA", "FDA approval"],
        specialist_focus="Apply healthcare domain expertise.",
    )
    profile = resolve_profile(custom)
    assert profile is custom
    assert profile.name == "healthcare"


# ── Built-in profiles ─────────────────────────────────────────────────


def test_finance_profile_contains_expected_vocabulary() -> None:
    assert FINANCE_PROFILE.name == "finance"
    assert "liquidity risk" in FINANCE_PROFILE.risk_vocabulary
    assert "execution risk" in FINANCE_PROFILE.risk_vocabulary
    assert any("market microstructure" in c for c in FINANCE_PROFILE.domain_constraints)


def test_software_profile_contains_expected_vocabulary() -> None:
    assert SOFTWARE_PROFILE.name == "software"
    assert "technical debt" in SOFTWARE_PROFILE.risk_vocabulary
    assert "API stability" in SOFTWARE_PROFILE.risk_vocabulary


def test_research_profile_contains_expected_vocabulary() -> None:
    assert RESEARCH_PROFILE.name == "research"
    assert "reproducibility" in RESEARCH_PROFILE.risk_vocabulary
    assert "statistical validity" in RESEARCH_PROFILE.risk_vocabulary


def test_default_profile_is_generic() -> None:
    assert DEFAULT_PROFILE.name == "default"
    assert len(DEFAULT_PROFILE.risk_vocabulary) > 0
    assert len(DEFAULT_PROFILE.specialist_focus) > 0


def test_all_builtin_profiles_have_required_shape() -> None:
    for profile in (
        FINANCE_PROFILE,
        SOFTWARE_PROFILE,
        RESEARCH_PROFILE,
        DEFAULT_PROFILE,
    ):
        assert isinstance(profile.name, str) and len(profile.name) > 0
        assert (
            isinstance(profile.risk_vocabulary, list)
            and len(profile.risk_vocabulary) > 0
        )
        assert (
            isinstance(profile.domain_constraints, list)
            and len(profile.domain_constraints) > 0
        )
        assert (
            isinstance(profile.specialist_focus, str)
            and len(profile.specialist_focus) > 0
        )


# ── init_debate with domain_profile ───────────────────────────────────


def test_init_debate_defaults_to_default_profile() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-def",
        input_version="1.0.0",
        artifact_content={},
    )
    assert state.domain_profile.name == "default"


def test_init_debate_uses_named_profile() -> None:
    state = init_debate(
        input_type="design-spec",
        input_id="spec-fin",
        input_version="1.0.0",
        artifact_content={},
        domain_profile="finance",
    )
    assert state.domain_profile.name == "finance"
    assert state.domain_profile is FINANCE_PROFILE


def test_init_debate_uses_custom_profile() -> None:
    custom = DomainProfile(
        name="biotech",
        risk_vocabulary=["regulatory risk", "clinical risk"],
        domain_constraints=["FDA", "IRB"],
        specialist_focus="biotech expertise",
    )
    state = init_debate(
        input_type="design-spec",
        input_id="spec-bio",
        input_version="1.0.0",
        artifact_content={},
        domain_profile=custom,
    )
    assert state.domain_profile.name == "biotech"
    assert state.domain_profile is custom


# ── handle_init_debate (artifact-once pattern) ────────────────────────


def _make_ctx() -> DebateContext:
    store: dict[str, DebateState | None] = {"value": None}

    def getter() -> DebateState | None:
        return store["value"]

    def setter(s: DebateState | None) -> None:
        store["value"] = s

    return DebateContext(get_active_debate=getter, set_active_debate=setter)


def test_handle_init_debate_includes_artifact_path_when_provided() -> None:
    ctx = _make_ctx()
    result = handle_init_debate(
        {
            "input_type": "design-spec",
            "input_id": "spec-handler-test",
            "input_version": "1.0.0",
            "artifact_content": {"spec_id": "spec-handler-test"},
            "artifact_path": "/tmp/artifacts/spec-handler-test.json",
        },
        ctx,
    )
    response = json.loads(result.json)
    assert response["artifact_path"] == "/tmp/artifacts/spec-handler-test.json"
    assert isinstance(response["agents"], list)
    assert len(response["agents"]) == 4


def test_handle_init_debate_omits_artifact_path_when_not_provided() -> None:
    ctx = _make_ctx()
    result = handle_init_debate(
        {
            "input_type": "design-spec",
            "input_id": "spec-no-path",
            "input_version": "1.0.0",
            "artifact_content": {"spec_id": "spec-no-path"},
        },
        ctx,
    )
    response = json.loads(result.json)
    assert "artifact_path" not in response


def test_handle_init_debate_prompts_do_not_contain_artifact_json() -> None:
    ctx = _make_ctx()
    artifact_content = {
        "spec_id": "spec-no-embed-handler",
        "unique_value": "EMBED_MARKER",
    }
    result = handle_init_debate(
        {
            "input_type": "design-spec",
            "input_id": "spec-no-embed-handler",
            "input_version": "1.0.0",
            "artifact_content": artifact_content,
            "artifact_path": "/tmp/spec-no-embed-handler.json",
        },
        ctx,
    )
    response = json.loads(result.json)
    for agent in response["agents"]:
        assert "EMBED_MARKER" not in agent["prompt"]
        assert "unique_value" not in agent["prompt"]


def test_handle_init_debate_includes_core_fields_and_agents() -> None:
    ctx = _make_ctx()
    result = handle_init_debate(
        {
            "input_type": "knowledge-overview",
            "input_id": "ov-handler-check",
            "input_version": "1.0.0",
            "artifact_content": {"overview_id": "ov-handler-check"},
        },
        ctx,
    )
    response = json.loads(result.json)
    assert isinstance(response["transcript_id"], str)
    assert response["transcript_id"].startswith("dt-")
    assert response["input_type"] == "knowledge-overview"
    assert response["input_id"] == "ov-handler-check"
    assert isinstance(response["agents"], list)
    assert len(response["agents"]) == 4


# ── resolve_debates_dir (Feature C) ───────────────────────────────────


def test_resolve_debates_dir_routes_to_run_data_dir() -> None:
    run_data_dir = "/tmp/pipeline-test/runs/my-run-2026-04-10"
    directory = resolve_debates_dir(run_data_dir, "/tmp/pipeline-test/legacy")
    assert directory == os.path.join(run_data_dir, "debates")


def test_resolve_debates_dir_falls_back_when_run_data_dir_none() -> None:
    legacy_base_dir = "/tmp/pipeline-test/legacy"
    directory = resolve_debates_dir(None, legacy_base_dir)
    assert directory == os.path.join(legacy_base_dir, "debates")


def test_resolve_debates_dir_empty_string_falls_back() -> None:
    legacy_base_dir = "/tmp/pipeline-test/legacy"
    directory = resolve_debates_dir("", legacy_base_dir)
    assert directory == os.path.join(legacy_base_dir, "debates")


# ── persist_debate (Feature C) ────────────────────────────────────────


def test_persist_debate_writes_under_run_data_dir() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "persist-debate-a")
        transcript = {
            "transcript_id": "dt-abc12345",
            "input_type": "design-spec",
            "input_id": "spec-x",
        }
        written_path = persist_debate(
            run_data_dir,
            os.path.join(temp_dir, "legacy"),
            transcript,
            "dt-abc12345.json",
        )
        expected = os.path.join(run_data_dir, "debates", "dt-abc12345.json")
        assert written_path == expected
        assert os.path.exists(expected), f"expected file at {expected}"
        parsed = json.loads(Path(expected).read_text(encoding="utf-8"))
        assert parsed["transcript_id"] == "dt-abc12345"
        assert parsed["input_type"] == "design-spec"
        assert parsed["input_id"] == "spec-x"


def test_persist_debate_falls_back_to_legacy_when_run_data_dir_absent() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        legacy_base_dir = os.path.join(temp_dir, "legacy-base-debates")
        transcript = {
            "transcript_id": "dt-legacy01",
            "input_type": "knowledge-overview",
            "input_id": "ov-y",
        }
        written_path = persist_debate(
            None,
            legacy_base_dir,
            transcript,
            "dt-legacy01.json",
        )
        expected = os.path.join(legacy_base_dir, "debates", "dt-legacy01.json")
        assert written_path == expected
        assert os.path.exists(expected), f"expected file at {expected}"
        parsed = json.loads(Path(expected).read_text(encoding="utf-8"))
        assert parsed["transcript_id"] == "dt-legacy01"


def test_persist_debate_falls_back_to_legacy_when_run_data_dir_empty() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        legacy_base_dir = os.path.join(temp_dir, "legacy-base-empty-rdd")
        transcript = {"transcript_id": "dt-empty01"}
        written_path = persist_debate(
            "", legacy_base_dir, transcript, "dt-empty01.json"
        )
        expected = os.path.join(
            legacy_base_dir, "debates", "dt-empty01.json"
        )
        assert written_path == expected
        assert os.path.exists(expected)


def test_persist_debate_creates_target_directory_tree() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "deep", "nested", "debate-target")
        transcript = {"transcript_id": "dt-deep0001"}
        persist_debate(run_data_dir, temp_dir, transcript, "dt-deep0001.json")
        directory = os.path.join(run_data_dir, "debates")
        assert os.path.exists(directory), f"expected mkdir to create {directory}"


def test_persist_debate_returns_absolute_path() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "abs-check")
        transcript = {"transcript_id": "dt-abs00001"}
        written_path = persist_debate(
            run_data_dir, temp_dir, transcript, "dt-abs00001.json"
        )
        assert os.path.isabs(
            written_path
        ), f"expected absolute path, got {written_path}"


def test_persist_debate_throws_when_file_exists_and_force_falsy() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "collision-test")
        first = {"transcript_id": "dt-collide1", "version": 1}
        second = {"transcript_id": "dt-collide1", "version": 2}
        first_path = persist_debate(run_data_dir, temp_dir, first, "dt-collide1.json")
        assert os.path.exists(first_path)
        with pytest.raises(
            FileExistsError,
            match=(
                r"(?s)Debate transcript already exists at .*dt-collide1\.json.*"
                r"Pass force=true to overwrite"
            ),
        ):
            persist_debate(run_data_dir, temp_dir, second, "dt-collide1.json")
        parsed = json.loads(Path(first_path).read_text(encoding="utf-8"))
        assert parsed["version"] == 1


def test_persist_debate_throws_when_file_exists_and_force_explicitly_false() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "collision-force-false")
        first = {"transcript_id": "dt-forcef01", "version": 1}
        second = {"transcript_id": "dt-forcef01", "version": 2}
        persist_debate(run_data_dir, temp_dir, first, "dt-forcef01.json", False)
        with pytest.raises(FileExistsError, match="Debate transcript already exists"):
            persist_debate(run_data_dir, temp_dir, second, "dt-forcef01.json", False)


def test_persist_debate_overwrites_when_force_true() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "force-overwrite")
        first = {"transcript_id": "dt-force001", "version": 1}
        second = {"transcript_id": "dt-force001", "version": 2}
        path1 = persist_debate(run_data_dir, temp_dir, first, "dt-force001.json")
        path2 = persist_debate(run_data_dir, temp_dir, second, "dt-force001.json", True)
        assert path1 == path2
        parsed = json.loads(Path(path2).read_text(encoding="utf-8"))
        assert parsed["version"] == 2


def test_persist_debate_sibling_files_both_persist() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "siblings")
        first = {"transcript_id": "dt-sib00001", "input_id": "a"}
        second = {"transcript_id": "dt-sib00002", "input_id": "b"}
        first_path = persist_debate(run_data_dir, temp_dir, first, "dt-sib00001.json")
        second_path = persist_debate(run_data_dir, temp_dir, second, "dt-sib00002.json")
        assert first_path == os.path.join(run_data_dir, "debates", "dt-sib00001.json")
        assert second_path == os.path.join(run_data_dir, "debates", "dt-sib00002.json")
        assert os.path.exists(first_path)
        assert os.path.exists(second_path)
        parsed1 = json.loads(Path(first_path).read_text(encoding="utf-8"))
        parsed2 = json.loads(Path(second_path).read_text(encoding="utf-8"))
        assert parsed1["input_id"] == "a"
        assert parsed2["input_id"] == "b"


def test_persist_debate_json_round_trips() -> None:
    with tempfile.TemporaryDirectory() as temp_dir:
        run_data_dir = os.path.join(temp_dir, "runs", "roundtrip")
        transcript = {
            "transcript_id": "dt-round001",
            "input_type": "knowledge-overview",
            "input_id": "ov-rt",
            "input_version": "1.0.0",
            "output_version": "1.1.0",
            "rounds": [
                {
                    "round": 1,
                    "type": "divergent",
                    "agents": [
                        {"role": "advocate", "position": "p", "confidence": 0.9}
                    ],
                },
            ],
            "synthesis": {"changes_accepted": ["a"], "changes_rejected": []},
        }
        written_path = persist_debate(
            run_data_dir, temp_dir, transcript, "dt-round001.json"
        )
        round_tripped = json.loads(
            Path(written_path).read_text(encoding="utf-8")
        )
        assert round_tripped == transcript
