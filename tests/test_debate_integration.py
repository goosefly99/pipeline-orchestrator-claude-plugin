"""Port of ``legacy-node/tests/debate-integration.test.ts`` (2 cases).

End-to-end debate flow over the REAL engine + REAL validator + REAL storage:
``init_debate`` → ``generate_agent_prompts`` → submit 4 arguments →
``generate_synthesis_prompt`` → ``build_transcript`` → ``validate_artifact``
against ``debate-transcript.json`` → ``store_artifact`` → ``load_artifact``.

The Node test reads ``../pipeline/schemas`` (relative to ``legacy-node/tests``);
the Python schemas live at ``src/pipeline_orchestrator/schemas/``, resolved here
OS-agnostically from ``REPO_ROOT`` (the ``Path(__file__)`` idiom used by
``test_validator.py``), not via hardcoded separators.
"""

from __future__ import annotations

from pathlib import Path

from pipeline_orchestrator.debate import (
    build_transcript,
    generate_agent_prompts,
    generate_synthesis_prompt,
    init_debate,
    submit_argument,
)
from pipeline_orchestrator.models import AgentArgument, DebateSynthesis
from pipeline_orchestrator.storage import (
    StorageConfig,
    load_artifact,
    store_artifact,
)
from pipeline_orchestrator.validator import load_schemas, validate_artifact

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "src" / "pipeline_orchestrator" / "schemas"


def test_integration_full_debate_flow_on_design_spec(tmp_path: Path) -> None:
    schemas = load_schemas(SCHEMAS_DIR)

    design_spec = {
        "spec_id": "spec-integration-test",
        "strategy_name": "Kalshi Event Arb",
        "version": "1.0.0",
        "created_date": "2026-04-03T14:00:00Z",
        "updated_date": "2026-04-03T14:00:00Z",
        "status": "draft",
        "overview_ids": ["ov-abc12345"],
        "trading_objective": "Capture mispricing in correlated Kalshi event markets",
        "market_selection": {
            "platforms": ["kalshi"],
            "criteria": "High-volume event markets with correlated outcomes",
            "target_markets": ["FOMC-2026-Q2", "FOMC-2026-Q3"],
        },
        "edge_hypothesis": "Correlated resolution events are mispriced relative to implied joint probability",  # noqa: E501
        "signal_inputs": [
            {"name": "market_price", "source": "kalshi_api", "type": "real_time"}
        ],
        "entry_rules": [{"condition": "spread > threshold", "action": "enter_long"}],
        "exit_rules": [{"condition": "spread normalises", "action": "exit"}],
        "risk_controls": {
            "max_position_size": 0.02,
            "max_daily_loss": 0.05,
            "stop_loss_pct": 0.1,
        },
        "position_sizing": {"method": "fixed_fraction", "base_fraction": 0.02},
        "implementation_notes": "Requires Kalshi API v2 with WebSocket feed",
    }

    state = init_debate(
        input_type="design-spec",
        input_id="spec-integration-test",
        input_version="1.0.0",
        artifact_content=design_spec,
    )

    assert state.transcript_id.startswith("dt-")
    assert len(state.round1_arguments) == 0

    prompts = generate_agent_prompts(state)
    assert len(prompts) == 4
    assert all("spec-integration-test" in p.prompt for p in prompts)

    state = submit_argument(
        state,
        AgentArgument(
            role="advocate",
            position="The edge hypothesis is grounded in established correlation mispricing literature.",  # noqa: E501
            evidence=["item-001", "item-002"],
            counterpoints=[],
            proposed_changes=[],
            confidence=0.85,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="critic",
            position="The entry rule threshold is undefined — this is not a deployable spec.",  # noqa: E501
            evidence=[],
            counterpoints=["No specific spread threshold value given"],
            proposed_changes=["Define threshold as a number or formula"],
            confidence=0.9,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="risk_specialist",
            position="Max daily loss 5% is appropriate but stop-loss 10% is inconsistent — stop should be tighter.",  # noqa: E501
            evidence=[],
            counterpoints=[
                "stop_loss_pct 0.1 implies losses larger than max_daily_loss in some scenarios"  # noqa: E501
            ],
            proposed_changes=[
                "Set stop_loss_pct to 0.03 or less to stay within max_daily_loss constraint"  # noqa: E501
            ],
            confidence=0.8,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="domain_specialist",
            position="Kalshi API v2 WebSocket is not publicly documented — verify feed availability before implementation.",  # noqa: E501
            evidence=[],
            counterpoints=[],
            proposed_changes=[
                "Validate WebSocket endpoint availability and rate limits before committing to implementation"  # noqa: E501
            ],
            confidence=0.75,
        ),
    )

    assert len(state.round1_arguments) == 4

    synth_prompt = generate_synthesis_prompt(state)
    assert "edge hypothesis is grounded" in synth_prompt
    assert "threshold is undefined" in synth_prompt
    assert "stop-loss" in synth_prompt
    assert "WebSocket" in synth_prompt

    state.synthesis = DebateSynthesis(
        changes_accepted=[
            "Define threshold as a specific spread value — accepted: core spec gap",
            "Validate WebSocket API availability before implementation — accepted: de-risks build phase",  # noqa: E501
            "Set stop_loss_pct to 0.03 — accepted: resolves logical inconsistency with max_daily_loss",  # noqa: E501
        ],
        changes_rejected=[
            {
                "proposed": "Reject the edge hypothesis entirely",
                "reason": "Advocate provided strong empirical backing; insufficient evidence to reject",  # noqa: E501
            }
        ],
        open_questions=[
            "What is the exact Kalshi API WebSocket endpoint and rate limit for live trading?",  # noqa: E501
            "What spread threshold is appropriate given Kalshi fee structure?",
        ],
    )
    state.output_version = "1.1.0"

    transcript = build_transcript(state)

    rounds = transcript["rounds"]
    assert isinstance(rounds, list)
    assert len(rounds) == 2
    assert rounds[0]["type"] == "divergent"
    assert len(rounds[0]["agents"]) == 4
    assert rounds[1]["type"] == "convergent"
    assert rounds[1]["agents"][0]["role"] == "synthesizer"
    synthesis = transcript["synthesis"]
    assert isinstance(synthesis, dict)
    assert len(synthesis["changes_accepted"]) == 3
    assert len(synthesis["changes_rejected"]) == 1
    assert len(synthesis["open_questions"]) == 2

    result = validate_artifact(schemas, "debate-transcript.json", transcript)
    assert result.valid is True, f"Schema validation failed: {', '.join(result.errors)}"

    sc = StorageConfig(base_dir=str(tmp_path), paths={"debates": "debates"})
    transcript_id = transcript["transcript_id"]
    assert isinstance(transcript_id, str)
    file_name = f"{transcript_id}.json"
    stored_path = store_artifact(sc, "debates", file_name, transcript)
    assert stored_path.endswith(file_name)

    loaded = load_artifact(sc, "debates", file_name)
    assert isinstance(loaded, dict)
    assert loaded["transcript_id"] == transcript_id
    assert loaded["input_id"] == "spec-integration-test"
    assert loaded["input_version"] == "1.0.0"
    assert loaded["output_version"] == "1.1.0"
    assert len(loaded["rounds"][0]["agents"]) == 4
    assert len(loaded["synthesis"]["changes_accepted"]) == 3


def test_integration_debate_flow_on_knowledge_overview() -> None:
    schemas = load_schemas(SCHEMAS_DIR)

    overview = {
        "overview_id": "ov-integration-test",
        "strategy_name": "Kalshi Rate Markets",
        "version": "1.0.0",
        "created_date": "2026-04-03T14:00:00Z",
        "updated_date": "2026-04-03T14:00:00Z",
        "status": "draft",
        "collection_ids": ["col-abc12345"],
        "key_concepts": [
            {
                "concept": "rate prediction",
                "confidence": 0.8,
                "supporting_items": ["item-001"],
            }
        ],
        "market_landscape": "Kalshi offers FOMC rate decision markets with binary resolution",  # noqa: E501
        "edge_opportunities": ["Anchoring bias in rate predictions", "Post-meeting drift"],  # noqa: E501
        "data_requirements": ["FOMC statements", "Fed funds futures"],
        "risks_and_constraints": ["Low liquidity post-resolution"],
        "research_gaps": ["Historical resolution data limited"],
    }

    state = init_debate(
        input_type="knowledge-overview",
        input_id="ov-integration-test",
        input_version="1.0.0",
        artifact_content=overview,
    )

    state = submit_argument(
        state,
        AgentArgument(
            role="advocate",
            position="Anchoring bias is well-documented.",
            confidence=0.8,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="critic",
            position="Edge opportunities lack quantification.",
            proposed_changes=["Add expected edge size estimates"],
            confidence=0.7,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="risk_specialist",
            position="Liquidity risk is correctly identified.",
            confidence=0.75,
        ),
    )
    state = submit_argument(
        state,
        AgentArgument(
            role="domain_specialist",
            position="FOMC binary markets have strict 24h resolution windows — factor into hold timing.",  # noqa: E501
            confidence=0.85,
        ),
    )

    state.synthesis = DebateSynthesis(
        changes_accepted=["Add expected edge size estimates — accepted: improves specificity"],  # noqa: E501
        changes_rejected=[],
        open_questions=[
            "Is 24h resolution window enforced strictly by Kalshi or is there grace period?"  # noqa: E501
        ],
    )
    state.output_version = "1.1.0"

    transcript = build_transcript(state)
    result = validate_artifact(schemas, "debate-transcript.json", transcript)
    assert result.valid is True, f"Schema validation failed: {', '.join(result.errors)}"
    assert transcript["input_type"] == "knowledge-overview"
    rounds = transcript["rounds"]
    assert isinstance(rounds, list)
    assert len(rounds[0]["agents"]) == 4
