"""Port of ``legacy-node/tests/pre-init-hook.test.ts`` (20 cases).

Byte-exact behavioral parity tests for ``run_pre_pipeline_init_hooks``
(:mod:`pipeline_orchestrator.hooks`), the ``pre_pipeline_init`` loader entry, and
``init_run`` run_parameters persistence. Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks:

* ``runPrePipelineInitHooks`` ×10 (in T2.5 scope — spawns the real ``node`` binary)
* ``loadHooksConfig — pre_pipeline_init trigger`` ×1 (loader lives in ``toml_loader``)
* ``handleInitRun — pre_pipeline_init integration`` ×6 → **skipped** (T6.2)
* ``run-state.initRun — run_parameters persistence`` ×3 (``init_run`` in ``run_state``)

The 6 ``handleInitRun`` integration cases drive ``handleInitRun`` from
``lifecycle-handlers.ts`` (the short-circuit / merge / persistence wiring around
the runner). That handler is a **T6.2** deliverable; ported 1:1 here but skipped
until ``handle_init_run`` lands. The underlying runner
(``run_pre_pipeline_init_hooks``) is fully covered by the unit cases above.

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` via the ``tmp_path`` fixture.
``run_pre_pipeline_init_hooks`` returns a :class:`PreInitHookResult` dataclass
(``.parameters`` / ``.userPrompts``) keyed exactly as the TS ``PreInitHookResult``
object. All paths are OS-agnostic (``os.path.join`` over ``tmp_path``).
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from pipeline_orchestrator.dag import InputSatisfaction
from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.hooks import (
    PreInitHookResult,
    run_pre_pipeline_init_hooks,
)
from pipeline_orchestrator.models import (
    DebateConfig,
    DebateOutputConfig,
    HookConfig,
    KBDefaults,
    PhaseDefinition,
    PipelineConfig,
    PipelineMeta,
    RunState,
    StorageConfig,
)
from pipeline_orchestrator.quality_gates import GateResult
from pipeline_orchestrator.run_state import (
    RecommendedAction,
    RecoveryResult,
    load_run_state,
)
from pipeline_orchestrator.run_state import add_artifact as add_artifact_state
from pipeline_orchestrator.run_state import init_run as init_run_state
from pipeline_orchestrator.run_state import skip_phase as skip_phase_state
from pipeline_orchestrator.toml_loader import load_hooks_config
from pipeline_orchestrator.tools.lifecycle_tools import (
    LifecycleContext,
    handle_init_run,
)

_NODE_PATH = shutil.which("node")
_NODE_REASON = "requires the node binary on PATH to run hook subprocesses"


def _node_hook_command() -> tuple[str, str]:
    """Return ``(project_root, command)`` for executing ``node`` as a hook."""
    assert _NODE_PATH is not None
    return os.path.dirname(_NODE_PATH), os.path.basename(_NODE_PATH)


def _make_context(temp_dir: str) -> dict[str, object]:
    """Return a ``PreInitHookContext``-shaped dict (mirrors Node ``makeContext``)."""
    return {
        "trigger": "pre_pipeline_init",
        "project_root": temp_dir,
        "requested_args": {"run_id": "test-run", "skip_phases": []},
    }


# ── runPrePipelineInitHooks ─────────────────────────────────


@pytest.mark.skipif(_NODE_PATH is None, reason=_NODE_REASON)
class TestRunPrePipelineInitHooks:
    def test_returns_empty_when_no_hooks_match(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        result = run_pre_pipeline_init_hooks([], _make_context(temp_dir), temp_dir)
        assert result.parameters == {}
        assert result.userPrompts == []

    def test_also_returns_empty_when_hooks_exist_but_none_match(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hooks = [
            HookConfig(trigger="pre_start", command="scripts/x.sh", phase_filter="*")
        ]
        result = run_pre_pipeline_init_hooks(
            hooks, _make_context(temp_dir), temp_dir
        )
        assert result.parameters == {}
        assert result.userPrompts == []

    def test_flows_parameters_from_a_hook_through_to_the_merged_result(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-params.mjs")
        Path(script_path).write_text(
            "process.stdout.write(JSON.stringify({parameters:"
            '{run_name:"test-run",phase_model:"claude-opus-4-6"}}))\n',
            encoding="utf-8",
        )

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        result = run_pre_pipeline_init_hooks(
            hooks, _make_context(temp_dir), hook_root
        )
        assert result.parameters["run_name"] == "test-run"
        assert result.parameters["phase_model"] == "claude-opus-4-6"
        assert result.userPrompts == []

    def test_flows_user_prompts_from_a_hook_through_to_the_merged_result(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-prompts.mjs")
        Path(script_path).write_text(
            "process.stdout.write(JSON.stringify({userPrompts:"
            '["What is run_name?"]}))\n',
            encoding="utf-8",
        )

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        result = run_pre_pipeline_init_hooks(
            hooks, _make_context(temp_dir), hook_root
        )
        assert result.userPrompts == ["What is run_name?"]
        assert result.parameters == {}

    def test_flows_both_parameters_and_user_prompts_from_a_single_hook(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-both.mjs")
        Path(script_path).write_text(
            "process.stdout.write(JSON.stringify({parameters:"
            '{phase_model:"x"},userPrompts:["confirm?"]}))\n',
            encoding="utf-8",
        )

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        result = run_pre_pipeline_init_hooks(
            hooks, _make_context(temp_dir), hook_root
        )
        assert result.parameters["phase_model"] == "x"
        assert result.userPrompts == ["confirm?"]

    def test_accepts_legacy_singular_user_prompt_alongside_plural(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-legacy.mjs")
        Path(script_path).write_text(
            'process.stdout.write(JSON.stringify({user_prompt:"legacy"}))\n',
            encoding="utf-8",
        )

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        result = run_pre_pipeline_init_hooks(
            hooks, _make_context(temp_dir), hook_root
        )
        assert result.userPrompts == ["legacy"]

    def test_throws_configuration_error_on_non_json_stdout(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-bad-json.mjs")
        Path(script_path).write_text(
            'process.stdout.write("not json here")\n', encoding="utf-8"
        )

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        with pytest.raises(PipelineError) as exc:
            run_pre_pipeline_init_hooks(hooks, _make_context(temp_dir), hook_root)
        assert exc.value.error_class == ErrorClass.configuration_error

    def test_throws_configuration_error_on_path_traversal(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hooks = [
            HookConfig(
                trigger="pre_pipeline_init", command="../evil.js", phase_filter="*"
            )
        ]

        with pytest.raises(PipelineError) as exc:
            run_pre_pipeline_init_hooks(hooks, _make_context(temp_dir), temp_dir)
        assert exc.value.error_class == ErrorClass.configuration_error

    def test_throws_configuration_error_on_non_zero_exit(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-exit1.mjs")
        Path(script_path).write_text("process.exit(1)\n", encoding="utf-8")

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        with pytest.raises(PipelineError) as exc:
            run_pre_pipeline_init_hooks(hooks, _make_context(temp_dir), hook_root)
        assert exc.value.error_class == ErrorClass.configuration_error

    def test_merges_parameters_across_multiple_hooks_later_hook_wins(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_a = os.path.join(temp_dir, "hook-a.mjs")
        script_b = os.path.join(temp_dir, "hook-b.mjs")
        Path(script_a).write_text(
            "process.stdout.write(JSON.stringify({parameters:"
            '{phase_model:"a"}}))\n',
            encoding="utf-8",
        )
        Path(script_b).write_text(
            "process.stdout.write(JSON.stringify({parameters:"
            '{phase_model:"b"}}))\n',
            encoding="utf-8",
        )

        hooks = [
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_a],
                timeout_ms=10000,
            ),
            HookConfig(
                trigger="pre_pipeline_init",
                command=node_command,
                args=[script_b],
                timeout_ms=10000,
            ),
        ]

        result = run_pre_pipeline_init_hooks(
            hooks, _make_context(temp_dir), hook_root
        )
        assert result.parameters["phase_model"] == "b"


# ── loadHooksConfig — pre_pipeline_init trigger ──────────────


class TestLoadHooksConfigPrePipelineInitTrigger:
    def test_accepts_pre_pipeline_init_as_a_valid_trigger_value(
        self, tmp_path: Path
    ) -> None:
        toml = """
[[hooks]]
trigger = "pre_pipeline_init"
command = "hooks/x.js"
"""
        toml_path = os.path.join(str(tmp_path), "hooks.toml")
        Path(toml_path).write_text(toml, encoding="utf-8")

        hooks = load_hooks_config(toml_path)
        assert len(hooks) == 1
        assert hooks[0].trigger == "pre_pipeline_init"
        assert hooks[0].command == "hooks/x.js"


# ── handle_init_run — pre_pipeline_init integration ─────────
#
# These 6 cases exercise the handler end-to-end with a mocked LifecycleContext.
# ``run_pre_pipeline_init_hooks`` is stubbed on the ctx so we control its return
# without spawning real hook scripts (the underlying runner is unit-tested
# above). The real ``init_run`` from run_state IS used so we can verify
# run_parameters are persisted to disk. Unskipped at T6.2; ported 1:1.


def _make_integration_config() -> PipelineConfig:
    """Single-entry-point (discovery) config (Node ``makeIntegrationConfig``)."""
    phases = {
        "discovery": PhaseDefinition(
            id=0,
            description="",
            inputs=[],
            outputs=[],
            tools=[],
            entry_point=True,
            optional=False,
        ),
    }
    return PipelineConfig(
        pipeline=PipelineMeta(id="test", version="1.0.0", description=""),
        phases=phases,
        edges=[],
        debate=DebateConfig(
            agents={},
            rounds={},
            output=DebateOutputConfig(
                includes_transcript=True,
                includes_refined_artifact=True,
                artifact_version_bump="minor",
            ),
        ),
        knowledge_bases=KBDefaults(
            available_in=[], max_queries_per_phase=20, query_mode="proactive"
        ),
        schemas={},
        storage=StorageConfig(base_dir="test", paths={}),
    )


class _IntegrationCtx:
    """Holder for an integration :class:`LifecycleContext` + an active-run ref."""

    def __init__(
        self,
        base_dir: str,
        hooks: list[HookConfig] | None = None,
        pre_init_result: PreInitHookResult | None = None,
        pre_init_throws: bool = False,
    ) -> None:
        self.active_run: list[RunState | None] = [None]
        self._active_run_dir = ""
        self._hooks = hooks if hooks is not None else []

        def _set_active_run(s: RunState) -> None:
            self.active_run[0] = s

        def _set_active_run_dir(d: str) -> None:
            self._active_run_dir = d

        def _run_pre_init(
            _hooks: list[Any], _ctx: dict[str, Any], _root: str
        ) -> PreInitHookResult:
            if pre_init_throws:
                raise PipelineError("hook failed", ErrorClass.configuration_error)
            return (
                pre_init_result
                if pre_init_result is not None
                else PreInitHookResult(parameters={}, userPrompts=[])
            )

        def _phase_input_sat(
            phase: PhaseDefinition, _types: list[str]
        ) -> InputSatisfaction:
            return InputSatisfaction(
                satisfied=[], missing=phase.inputs, can_run=False
            )

        self.ctx = LifecycleContext(
            get_active_run=lambda: self.active_run[0],
            set_active_run=_set_active_run,
            get_active_run_dir=lambda: self._active_run_dir,
            set_active_run_dir=_set_active_run_dir,
            get_config=lambda: _make_integration_config(),
            set_project_root=lambda _root: None,
            get_storage_config=lambda *_a, **_k: StorageConfig(
                base_dir=base_dir, paths={}
            ),
            init_run=lambda r_id, pv, phases, state_dir, rp=None: init_run_state(
                r_id, pv, phases, state_dir, rp
            ),
            skip_phase=lambda s, phase, d: skip_phase_state(s, phase, d),
            add_artifact=lambda s, ref, d: add_artifact_state(s, ref, d),
            start_phase=lambda s, _p, _d: s,
            complete_phase=lambda s, _p, _d: s,
            fail_phase=lambda s, _p, _e, _d: s,
            retry_phase=lambda s, _p, _d: s,
            resolve_next_phases=lambda *_a: [],
            get_phase_input_satisfaction=_phase_input_sat,
            load_run_state=lambda _d: None,
            recover_run=lambda _d: RecoveryResult(
                state=self.active_run[0], recovered_phases=[], warnings=[]  # type: ignore[arg-type]
            ),
            remove_phase_artifacts=lambda *_a: [],
            get_quality_gates=lambda: [],
            run_gate_checks=lambda *_a: GateResult(
                phase="", passed=True, on_failure="warn", results=[]
            ),
            get_hooks_config=lambda: self._hooks,
            run_hooks=lambda *_a: [],
            run_pre_pipeline_init_hooks=_run_pre_init,
            get_project_root=lambda: None,
            compute_recommended_action=lambda *_a: RecommendedAction(
                action="review", reason=""
            ),
            compute_run_warnings=lambda *_a, **_k: [],
        )


class TestHandleInitRunPrePipelineInitIntegration:
    def test_no_pre_init_hooks_and_no_user_params_baseline(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        rec = _IntegrationCtx(temp_dir)

        result = handle_init_run(
            {"run_id": "base-run", "project_root": temp_dir}, rec.ctx
        )
        parsed = json.loads(result.json)

        assert parsed["run_id"] == "base-run"
        assert parsed["run_parameters"] == {}

        disk = load_run_state(os.path.join(temp_dir, "runs", "base-run"))
        assert disk is not None
        assert disk.run_parameters is None

    def test_no_pre_init_hooks_with_explicit_run_parameters(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        rec = _IntegrationCtx(temp_dir)

        result = handle_init_run(
            {
                "run_id": "explicit-run",
                "project_root": temp_dir,
                "run_parameters": {
                    "run_name": "explicit-test",
                    "phase_model": "sonnet-4-6",
                },
            },
            rec.ctx,
        )
        parsed = json.loads(result.json)

        assert parsed["run_parameters"]["run_name"] == "explicit-test"
        assert parsed["run_parameters"]["phase_model"] == "sonnet-4-6"

        disk = load_run_state(os.path.join(temp_dir, "runs", "explicit-run"))
        assert disk is not None
        assert disk.run_parameters == {
            "run_name": "explicit-test",
            "phase_model": "sonnet-4-6",
        }

    def test_pre_init_hook_emits_parameters_and_no_user_override(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        rec = _IntegrationCtx(
            temp_dir,
            hooks=[
                HookConfig(
                    trigger="pre_pipeline_init", command="hooks/anything.js"
                )
            ],
            pre_init_result=PreInitHookResult(
                parameters={"run_name": "hook-default", "phase_model": "opus"},
                userPrompts=[],
            ),
        )

        result = handle_init_run(
            {"run_id": "hook-run", "project_root": temp_dir}, rec.ctx
        )
        parsed = json.loads(result.json)

        assert parsed["run_parameters"]["run_name"] == "hook-default"
        assert parsed["run_parameters"]["phase_model"] == "opus"

        disk = load_run_state(os.path.join(temp_dir, "runs", "hook-run"))
        assert disk is not None
        assert disk.run_parameters is not None
        assert disk.run_parameters["run_name"] == "hook-default"
        assert disk.run_parameters["phase_model"] == "opus"

    def test_pre_init_hook_parameters_merged_with_explicit_args_user_wins(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        rec = _IntegrationCtx(
            temp_dir,
            hooks=[
                HookConfig(
                    trigger="pre_pipeline_init", command="hooks/anything.js"
                )
            ],
            pre_init_result=PreInitHookResult(
                parameters={"phase_model": "opus", "target_feature": "from-hook"},
                userPrompts=[],
            ),
        )

        result = handle_init_run(
            {
                "run_id": "merge-run",
                "project_root": temp_dir,
                "run_parameters": {"phase_model": "sonnet"},
            },
            rec.ctx,
        )
        parsed = json.loads(result.json)

        # User's phase_model wins; hook's target_feature is preserved.
        assert parsed["run_parameters"]["phase_model"] == "sonnet"
        assert parsed["run_parameters"]["target_feature"] == "from-hook"

        disk = load_run_state(os.path.join(temp_dir, "runs", "merge-run"))
        assert disk is not None
        assert disk.run_parameters is not None
        assert disk.run_parameters["phase_model"] == "sonnet"
        assert disk.run_parameters["target_feature"] == "from-hook"

    def test_pre_init_hook_requests_user_input_with_no_params_short_circuits(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        rec = _IntegrationCtx(
            temp_dir,
            hooks=[
                HookConfig(
                    trigger="pre_pipeline_init", command="hooks/anything.js"
                )
            ],
            pre_init_result=PreInitHookResult(
                parameters={}, userPrompts=["What is run_name?"]
            ),
        )

        result = handle_init_run(
            {"run_id": "short-circuit-run", "project_root": temp_dir}, rec.ctx
        )
        envelope = json.loads(result.json)

        assert envelope["status"] == "ok"
        assert envelope["data"]["status"] == "user_input_required"
        assert envelope["data"]["prompts"] == ["What is run_name?"]
        assert envelope["data"]["partial_parameters"] == {}
        assert "run_parameters" in envelope["next_step"]

        # Critical: NO run state was written to disk.
        disk_path = os.path.join(
            temp_dir, "runs", "short-circuit-run", "run-state.json"
        )
        assert not os.path.exists(disk_path)

        # And the active run on the ctx must still be None.
        assert rec.active_run[0] is None

    def test_pre_init_hook_requests_user_input_but_params_provided_proceeds(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        rec = _IntegrationCtx(
            temp_dir,
            hooks=[
                HookConfig(
                    trigger="pre_pipeline_init", command="hooks/anything.js"
                )
            ],
            pre_init_result=PreInitHookResult(
                parameters={}, userPrompts=["What is run_name?"]
            ),
        )

        result = handle_init_run(
            {
                "run_id": "supplied-run",
                "project_root": temp_dir,
                "run_parameters": {"run_name": "supplied"},
            },
            rec.ctx,
        )
        parsed = json.loads(result.json)

        assert parsed["run_id"] == "supplied-run"
        assert parsed["run_parameters"]["run_name"] == "supplied"

        disk = load_run_state(os.path.join(temp_dir, "runs", "supplied-run"))
        assert disk is not None
        assert disk.run_parameters is not None
        assert disk.run_parameters["run_name"] == "supplied"


# ── run-state.initRun — run_parameters persistence ──────────


class TestInitRunRunParametersPersistence:
    def test_persists_run_parameters_when_non_empty_object_is_provided(
        self, tmp_path: Path
    ) -> None:
        state_dir = os.path.join(str(tmp_path), "run-params-set")
        state = init_run_state(
            "p1", "1.0.0", ["discovery"], state_dir, {"run_name": "test"}
        )

        assert state.run_parameters == {"run_name": "test"}

        # Verify the persisted JSON on disk has run_parameters too.
        raw = Path(os.path.join(state_dir, "run-state.json")).read_text(
            encoding="utf-8"
        )
        parsed = json.loads(raw)
        assert parsed["run_parameters"] == {"run_name": "test"}

    def test_omits_run_parameters_from_state_when_empty_object_is_provided(
        self, tmp_path: Path
    ) -> None:
        state_dir = os.path.join(str(tmp_path), "run-params-empty")
        state = init_run_state("p2", "1.0.0", ["discovery"], state_dir, {})

        assert state.run_parameters is None

        # The persisted JSON on disk must NOT have a run_parameters key either.
        raw = Path(os.path.join(state_dir, "run-state.json")).read_text(
            encoding="utf-8"
        )
        parsed = json.loads(raw)
        assert parsed.get("run_parameters") is None
        assert ("run_parameters" in parsed) is False

    def test_omits_run_parameters_from_state_when_argument_is_undefined(
        self, tmp_path: Path
    ) -> None:
        state_dir = os.path.join(str(tmp_path), "run-params-undef")
        state = init_run_state("p3", "1.0.0", ["discovery"], state_dir)

        assert state.run_parameters is None

        raw = Path(os.path.join(state_dir, "run-state.json")).read_text(
            encoding="utf-8"
        )
        parsed = json.loads(raw)
        assert ("run_parameters" in parsed) is False
