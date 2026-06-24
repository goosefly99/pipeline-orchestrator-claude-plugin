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

import pytest

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.hooks import run_pre_pipeline_init_hooks
from pipeline_orchestrator.models import HookConfig
from pipeline_orchestrator.run_state import init_run as init_run_state
from pipeline_orchestrator.toml_loader import load_hooks_config

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


# ── handleInitRun — pre_pipeline_init integration ───────────
#
# These 6 cases drive ``handleInitRun`` from ``lifecycle-handlers.ts`` — the
# short-circuit / parameter-merge / persistence wiring around the pre-init
# runner (including the ``user_input_required`` envelope and the no-state-on-
# short-circuit invariant). That handler is a **T6.2** deliverable; ported 1:1
# here but skipped until ``handle_init_run`` (and its mockable LifecycleContext)
# land. The runner they wrap (``run_pre_pipeline_init_hooks``) is fully covered
# by the unit cases above.

_HANDLE_INIT_RUN_REASON = (
    "T6.2: handleInitRun (pre_pipeline_init short-circuit + parameter-merge + "
    "persistence wiring, with a mockable LifecycleContext) lives in the "
    "lifecycle-handler layer (lifecycle-handlers.ts), not the T2.5 hooks seam. "
    "Ported 1:1 and unskipped when lifecycle_handlers lands."
)


@pytest.mark.skip(reason=_HANDLE_INIT_RUN_REASON)
class TestHandleInitRunPrePipelineInitIntegration:
    def test_no_pre_init_hooks_and_no_user_params_baseline(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_HANDLE_INIT_RUN_REASON)

    def test_no_pre_init_hooks_with_explicit_run_parameters(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_HANDLE_INIT_RUN_REASON)

    def test_pre_init_hook_emits_parameters_and_no_user_override(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_HANDLE_INIT_RUN_REASON)

    def test_pre_init_hook_parameters_merged_with_explicit_args_user_wins(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_HANDLE_INIT_RUN_REASON)

    def test_pre_init_hook_requests_user_input_with_no_params_short_circuits(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_HANDLE_INIT_RUN_REASON)

    def test_pre_init_hook_requests_user_input_but_params_provided_proceeds(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_HANDLE_INIT_RUN_REASON)


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
