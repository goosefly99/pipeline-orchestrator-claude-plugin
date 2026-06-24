"""Port of ``legacy-node/tests/hooks.test.ts`` (39 cases).

Byte-exact behavioral parity tests for the hooks subprocess seam
(:mod:`pipeline_orchestrator.hooks`). Each Node ``it(...)`` maps 1:1 to a
``test_*`` method here, grouped into classes mirroring the Node ``describe``
blocks:

* ``validateHookPath`` ×4
* ``matchesPhase`` ×4
* ``runHooks`` ×6
* ``parseAgentDirective`` ×15
* ``runHooks agent_directive passthrough`` ×2 (spawns the real ``node`` binary)
* ``appendEvent`` ×4 → **skipped** (T6.2 lifecycle-handlers)
* ``loadHooksConfig`` ×4 (the loader lives in ``toml_loader``)
* ``model_tier in PhaseDefinition`` ×1 (drives ``load_pipeline_config``)

The 4 ``appendEvent`` cases exercise the events.jsonl writer from
``lifecycle-handlers.ts``, which is a **T6.2** deliverable; they are ported 1:1
but skipped until ``append_event`` lands. The two ``agent_directive`` passthrough
cases mirror the Node ``nodeHookCommand`` helper: ``project_root`` is the
directory of the running ``node`` binary, ``command`` its basename, and the real
hook script is passed via ``args[0]``.

Fixtures mirror the Node ``beforeEach``/``afterEach`` ``mkdtempSync``/``rmSync``
via the ``tmp_path`` fixture. All paths are OS-agnostic (``os.path.join`` over
``tmp_path``) — never hardcoded slashes.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from pipeline_orchestrator.errors import ErrorClass, PipelineError
from pipeline_orchestrator.hooks import (
    matches_phase,
    parse_agent_directive,
    run_hooks,
    validate_hook_path,
)
from pipeline_orchestrator.models import HookConfig
from pipeline_orchestrator.toml_loader import load_hooks_config, load_pipeline_config

# Resolve the node binary the way the Node ``nodeHookCommand`` helper does:
# project_root = dirname(node), command = basename(node). Script files live in
# the tmp dir and are passed as args[0].
_NODE_PATH = shutil.which("node")


def _node_hook_command() -> tuple[str, str]:
    """Return ``(project_root, command)`` for executing ``node`` as a hook.

    Mirrors the Node ``nodeHookCommand`` helper: ``project_root`` is the dir
    containing the running node binary, ``command`` is its basename. This lets
    ``validate_hook_path`` accept the node binary as a hook command.
    """
    assert _NODE_PATH is not None
    return os.path.dirname(_NODE_PATH), os.path.basename(_NODE_PATH)


_NODE_REASON = "requires the node binary on PATH to run hook subprocesses"


# ── validateHookPath ─────────────────────────────────────────


class TestValidateHookPath:
    def test_resolves_a_relative_path_against_project_root(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        script_dir = os.path.join(project_root, "scripts")
        os.makedirs(script_dir, exist_ok=True)

        resolved = validate_hook_path("scripts/test.sh", project_root)
        assert resolved == os.path.join(project_root, "scripts", "test.sh")

    def test_accepts_an_absolute_path_within_project_root(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)
        script_path = os.path.join(project_root, "test.sh")

        resolved = validate_hook_path(script_path, project_root)
        assert resolved == script_path

    def test_rejects_paths_with_dotdot_traversal(self, tmp_path: Path) -> None:
        project_root = str(tmp_path)

        with pytest.raises(PipelineError) as exc:
            validate_hook_path("../evil.sh", project_root)
        assert exc.value.error_class == ErrorClass.configuration_error

    def test_rejects_paths_with_embedded_dotdot_after_normalization(
        self, tmp_path: Path
    ) -> None:
        project_root = str(tmp_path)

        with pytest.raises(PipelineError) as exc:
            validate_hook_path("scripts/../../evil.sh", project_root)
        assert exc.value.error_class == ErrorClass.configuration_error


# ── matchesPhase ─────────────────────────────────────────────


class TestMatchesPhase:
    def test_returns_true_for_wildcard_filter(self) -> None:
        assert matches_phase("*", "discovery") is True

    def test_returns_true_for_undefined_filter(self) -> None:
        assert matches_phase(None, "discovery") is True

    def test_returns_true_for_exact_match(self) -> None:
        assert matches_phase("discovery", "discovery") is True

    def test_returns_false_for_non_matching_filter(self) -> None:
        assert matches_phase("curation", "discovery") is False


# ── runHooks ─────────────────────────────────────────────────


class TestRunHooks:
    def test_returns_empty_array_when_no_hooks_match(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        hooks = [
            HookConfig(
                trigger="post_complete", command="echo", args=["hi"], phase_filter="*"
            )
        ]
        ctx = {
            "run_id": "r1",
            "phase": "discovery",
            "trigger": "pre_start",
            "project_root": temp_dir,
            "run_dir": temp_dir,
        }

        results = run_hooks(hooks, "pre_start", ctx, temp_dir)
        assert results == []

    def test_returns_empty_array_when_no_hooks_are_configured(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        ctx = {
            "run_id": "r1",
            "phase": "discovery",
            "trigger": "pre_start",
            "project_root": temp_dir,
            "run_dir": temp_dir,
        }
        results = run_hooks([], "pre_start", ctx, temp_dir)
        assert results == []

    def test_filters_hooks_by_phase_filter(self, tmp_path: Path) -> None:
        temp_dir = str(tmp_path)
        hooks = [
            HookConfig(
                trigger="post_complete",
                command="echo",
                args=["hi"],
                phase_filter="curation",
            )
        ]
        ctx = {
            "run_id": "r1",
            "phase": "discovery",
            "trigger": "post_complete",
            "project_root": temp_dir,
            "run_dir": temp_dir,
        }

        results = run_hooks(hooks, "post_complete", ctx, temp_dir)
        assert results == []

    def test_throws_pipeline_error_for_pre_start_hook_path_validation_failure(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hooks = [
            HookConfig(trigger="pre_start", command="../evil.sh", phase_filter="*")
        ]
        ctx = {
            "run_id": "r1",
            "phase": "discovery",
            "trigger": "pre_start",
            "project_root": temp_dir,
            "run_dir": temp_dir,
        }

        with pytest.raises(PipelineError) as exc:
            run_hooks(hooks, "pre_start", ctx, temp_dir)
        assert exc.value.error_class == ErrorClass.configuration_error

    def test_records_failure_for_non_blocking_hook_path_validation_failure(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hooks = [
            HookConfig(trigger="post_complete", command="../evil.sh", phase_filter="*")
        ]
        ctx = {
            "run_id": "r1",
            "phase": "discovery",
            "trigger": "post_complete",
            "project_root": temp_dir,
            "run_dir": temp_dir,
        }

        results = run_hooks(hooks, "post_complete", ctx, temp_dir)
        assert len(results) == 1
        assert results[0]["success"] is False
        assert "traversal" in results[0]["error"]


# ── parseAgentDirective (Feature B B2) ──────────────────────

_VALID_DIRECTIVE = {
    "agent_directive": {
        "subagent_type": "general-purpose",
        "model": "claude-sonnet-4-6",
        "description": "discovery execution",
        "prompt": "Run phase discovery and call pipeline_complete_phase when done.",
    },
}


class TestParseAgentDirective:
    def test_returns_undefined_for_undefined_stdout(self) -> None:
        assert parse_agent_directive(None) is None

    def test_returns_undefined_for_empty_string(self) -> None:
        assert parse_agent_directive("") is None

    def test_returns_undefined_for_whitespace_only_stdout(self) -> None:
        assert parse_agent_directive("   \n  \t ") is None

    def test_returns_undefined_for_non_json_stdout(self) -> None:
        assert parse_agent_directive("hello world") is None

    def test_returns_undefined_for_json_that_is_not_an_object(self) -> None:
        assert parse_agent_directive('"just a string"') is None
        assert parse_agent_directive("42") is None
        assert parse_agent_directive("null") is None
        assert parse_agent_directive("[1, 2, 3]") is None

    def test_returns_undefined_when_agent_directive_key_is_missing(self) -> None:
        assert parse_agent_directive('{"parameters": {}}') is None

    def test_returns_undefined_when_agent_directive_is_null(self) -> None:
        assert parse_agent_directive('{"agent_directive": null}') is None

    def test_returns_undefined_when_agent_directive_is_not_an_object(self) -> None:
        assert parse_agent_directive('{"agent_directive": "nope"}') is None
        assert parse_agent_directive('{"agent_directive": 7}') is None

    def test_returns_undefined_when_any_required_field_is_missing(self) -> None:
        for field_name in ["subagent_type", "model", "description", "prompt"]:
            clone = json.loads(json.dumps(_VALID_DIRECTIVE))
            del clone["agent_directive"][field_name]
            assert parse_agent_directive(json.dumps(clone)) is None, (
                f"missing {field_name} should fail"
            )

    def test_returns_undefined_when_any_required_field_is_not_a_string(self) -> None:
        for field_name in ["subagent_type", "model", "description", "prompt"]:
            clone = json.loads(json.dumps(_VALID_DIRECTIVE))
            clone["agent_directive"][field_name] = 42
            assert parse_agent_directive(json.dumps(clone)) is None, (
                f"numeric {field_name} should fail"
            )

    def test_parses_a_valid_directive_with_no_isolation(self) -> None:
        result = parse_agent_directive(json.dumps(_VALID_DIRECTIVE))
        assert result == {
            "subagent_type": "general-purpose",
            "model": "claude-sonnet-4-6",
            "description": "discovery execution",
            "prompt": "Run phase discovery and call pipeline_complete_phase when done.",
        }

    def test_preserves_isolation_worktree_when_present(self) -> None:
        with_worktree = json.loads(json.dumps(_VALID_DIRECTIVE))
        with_worktree["agent_directive"]["isolation"] = "worktree"
        result = parse_agent_directive(json.dumps(with_worktree))
        assert result is not None
        assert result.get("isolation") == "worktree"

    def test_drops_non_worktree_isolation_values(self) -> None:
        with_other = json.loads(json.dumps(_VALID_DIRECTIVE))
        with_other["agent_directive"]["isolation"] = "sandbox"
        result = parse_agent_directive(json.dumps(with_other))
        assert result is not None
        assert result.get("isolation") is None

    def test_tolerates_surrounding_whitespace_around_the_json(self) -> None:
        padded = f"\n\n  {json.dumps(_VALID_DIRECTIVE)}  \n"
        result = parse_agent_directive(padded)
        assert result is not None
        assert result.get("subagent_type") == "general-purpose"

    def test_ignores_extra_unknown_fields_on_the_directive(self) -> None:
        with_extra = json.loads(json.dumps(_VALID_DIRECTIVE))
        with_extra["agent_directive"]["debug"] = True
        with_extra["agent_directive"]["extra_note"] = "ignored"
        result = parse_agent_directive(json.dumps(with_extra))
        # Only the four required fields should be forwarded — extras are dropped.
        assert result == {
            "subagent_type": "general-purpose",
            "model": "claude-sonnet-4-6",
            "description": "discovery execution",
            "prompt": "Run phase discovery and call pipeline_complete_phase when done.",
        }


# ── runHooks attaches agent_directive from stdout JSON ─────


@pytest.mark.skipif(_NODE_PATH is None, reason=_NODE_REASON)
class TestRunHooksAgentDirectivePassthrough:
    def test_attaches_agent_directive_when_pre_start_hook_emits_valid_json(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "pre-start-directive.mjs")
        payload = json.dumps(
            {
                "agent_directive": {
                    "subagent_type": "general-purpose",
                    "model": "claude-opus-4-6",
                    "description": "debate execution",
                    "prompt": "Run debate and call pipeline_complete_phase on finish.",
                },
            }
        )
        Path(script_path).write_text(
            f"console.log({json.dumps(payload)})\n", encoding="utf-8"
        )

        hooks = [
            HookConfig(
                trigger="pre_start",
                phase_filter="*",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]
        ctx = {
            "run_id": "r1",
            "phase": "debate",
            "trigger": "pre_start",
            "project_root": hook_root,
            "run_dir": temp_dir,
        }

        results = run_hooks(hooks, "pre_start", ctx, hook_root)
        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0]["agent_directive"] == {
            "subagent_type": "general-purpose",
            "model": "claude-opus-4-6",
            "description": "debate execution",
            "prompt": "Run debate and call pipeline_complete_phase on finish.",
        }

    def test_leaves_agent_directive_undefined_when_hook_emits_plain_text_stdout(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_command = _node_hook_command()
        script_path = os.path.join(temp_dir, "plain-stdout.mjs")
        Path(script_path).write_text(
            'console.log("hello from hook")\n', encoding="utf-8"
        )

        hooks = [
            HookConfig(
                trigger="pre_start",
                phase_filter="*",
                command=node_command,
                args=[script_path],
                timeout_ms=10000,
            )
        ]
        ctx = {
            "run_id": "r1",
            "phase": "discovery",
            "trigger": "pre_start",
            "project_root": hook_root,
            "run_dir": temp_dir,
        }

        results = run_hooks(hooks, "pre_start", ctx, hook_root)
        assert len(results) == 1
        assert results[0]["success"] is True
        assert results[0].get("agent_directive") is None
        assert results[0]["stdout"] == "hello from hook"


# ── appendEvent ──────────────────────────────────────────────
#
# These 4 cases exercise ``appendEvent`` from ``lifecycle-handlers.ts`` — the
# events.jsonl audit-trail writer. That writer is a **T6.2** deliverable
# (lifecycle-handler layer); it is ported here 1:1 but skipped until
# ``append_event`` lands. The hooks seam under test (``run_hooks``) reads
# events.jsonl directly via its idempotency guard, which is covered by
# ``test_hook_idempotency.py``.

_APPEND_EVENT_REASON = (
    "T6.2: appendEvent (events.jsonl writer) lives in the lifecycle-handler "
    "layer (lifecycle-handlers.ts), not the T2.5 hooks seam. Ported 1:1 and "
    "unskipped when lifecycle_handlers lands."
)


@pytest.mark.skip(reason=_APPEND_EVENT_REASON)
class TestAppendEvent:
    def test_creates_events_jsonl_and_appends_a_single_event(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_APPEND_EVENT_REASON)

    def test_appends_multiple_events_as_separate_lines(self, tmp_path: Path) -> None:
        raise NotImplementedError(_APPEND_EVENT_REASON)

    def test_creates_run_directory_if_it_does_not_exist(
        self, tmp_path: Path
    ) -> None:
        raise NotImplementedError(_APPEND_EVENT_REASON)

    def test_includes_details_when_provided(self, tmp_path: Path) -> None:
        raise NotImplementedError(_APPEND_EVENT_REASON)


# ── loadHooksConfig (TOML parsing) ───────────────────────────


class TestLoadHooksConfig:
    def test_returns_empty_array_for_nonexistent_file(self, tmp_path: Path) -> None:
        hooks = load_hooks_config(os.path.join(str(tmp_path), "nonexistent.toml"))
        assert hooks == []

    def test_parses_valid_hooks_configuration(self, tmp_path: Path) -> None:
        toml = """
[[hooks]]
trigger = "pre_start"
phase_filter = "*"
command = "scripts/validate-env.sh"
args = ["--strict"]
timeout_ms = 3000

[[hooks]]
trigger = "post_complete"
phase_filter = "curation"
command = "scripts/notify.sh"
"""
        toml_path = os.path.join(str(tmp_path), "hooks.toml")
        Path(toml_path).write_text(toml, encoding="utf-8")

        hooks = load_hooks_config(toml_path)
        assert len(hooks) == 2

        assert hooks[0].trigger == "pre_start"
        assert hooks[0].phase_filter == "*"
        assert hooks[0].command == "scripts/validate-env.sh"
        assert hooks[0].args == ["--strict"]
        assert hooks[0].timeout_ms == 3000

        assert hooks[1].trigger == "post_complete"
        assert hooks[1].phase_filter == "curation"

    def test_defaults_phase_filter_to_star_and_timeout_ms_to_5000(
        self, tmp_path: Path
    ) -> None:
        toml = """
[[hooks]]
trigger = "on_fail"
command = "scripts/cleanup.sh"
"""
        toml_path = os.path.join(str(tmp_path), "hooks.toml")
        Path(toml_path).write_text(toml, encoding="utf-8")

        hooks = load_hooks_config(toml_path)
        assert len(hooks) == 1
        assert hooks[0].phase_filter == "*"
        assert hooks[0].timeout_ms == 5000

    def test_skips_hooks_with_invalid_trigger_values(self, tmp_path: Path) -> None:
        toml = """
[[hooks]]
trigger = "invalid_trigger"
command = "scripts/test.sh"

[[hooks]]
trigger = "pre_start"
command = "scripts/valid.sh"
"""
        toml_path = os.path.join(str(tmp_path), "hooks.toml")
        Path(toml_path).write_text(toml, encoding="utf-8")

        hooks = load_hooks_config(toml_path)
        assert len(hooks) == 1
        assert hooks[0].trigger == "pre_start"


# ── model_tier parsing ───────────────────────────────────────


class TestModelTierInPhaseDefinition:
    def test_model_tier_is_surfaced_in_pipeline_get_config_response(self) -> None:
        # This verifies the type includes model_tier via toml-loader.
        pipeline_dir = Path(__file__).resolve().parent.parent / "src" / (
            "pipeline_orchestrator"
        ) / "pipeline"
        config = load_pipeline_config(str(pipeline_dir / "pipeline.toml"))

        # All phases should load (model_tier may be undefined for phases without it).
        for name, phase in config.phases.items():
            assert phase.id is not None, f'Phase "{name}" should have an id'
            # model_tier can be None (not yet set in pipeline.toml) — that's valid.
            if phase.model_tier is not None:
                assert phase.model_tier in ("haiku", "sonnet", "opus"), (
                    f'Phase "{name}" model_tier must be haiku, sonnet, or opus'
                )
