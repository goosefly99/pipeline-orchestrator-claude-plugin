"""Port of ``legacy-node/tests/pre-start-agent-directive.test.ts`` (6 cases).

Byte-exact behavioral parity tests for Feature B's pre_start ``agent_directive``
parsing/passthrough (:mod:`pipeline_orchestrator.hooks`). Each Node ``it(...)``
maps 1:1 to a ``test_*`` method here, grouped into classes mirroring the Node
``describe`` blocks:

* ``parseAgentDirective`` ×4 (pure parser)
* ``runHooks pre_start with agent_directive`` ×2 (spawns the real ``node`` binary)

The Node file's ``handleStartPhase`` integration cases (precedence + omission)
referenced in its header docstring are not present as ``it(...)`` blocks in the
source — only these six unit cases are. The two ``runHooks`` cases mirror the
``nodeHookCommand`` helper: ``project_root`` is the dir of the running ``node``
binary, ``command`` its basename, the hook script passed via ``args[0]``.

Fixtures mirror the Node ``mkdtempSync``/``rmSync`` via the ``tmp_path`` fixture.
All paths are OS-agnostic (``os.path.join`` over ``tmp_path``).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path

import pytest

from pipeline_orchestrator.hooks import parse_agent_directive, run_hooks
from pipeline_orchestrator.models import HookConfig

_NODE_PATH = shutil.which("node")
_NODE_REASON = "requires the node binary on PATH to run hook subprocesses"


def _node_hook_command() -> tuple[str, str]:
    """Return ``(project_root, command)`` for executing ``node`` as a hook."""
    assert _NODE_PATH is not None
    return os.path.dirname(_NODE_PATH), os.path.basename(_NODE_PATH)


class TestParseAgentDirective:
    def test_returns_a_directive_for_well_formed_json(self) -> None:
        stdout = json.dumps(
            {
                "agent_directive": {
                    "subagent_type": "general-purpose",
                    "model": "claude-sonnet-4-6",
                    "description": "test phase",
                    "prompt": "do the thing",
                },
            }
        )
        d = parse_agent_directive(stdout)
        assert d
        assert d["subagent_type"] == "general-purpose"
        assert d["model"] == "claude-sonnet-4-6"

    def test_preserves_optional_isolation_when_present(self) -> None:
        stdout = json.dumps(
            {
                "agent_directive": {
                    "subagent_type": "general-purpose",
                    "model": "claude-opus-4-6",
                    "description": "worktree phase",
                    "prompt": "do the thing",
                    "isolation": "worktree",
                },
            }
        )
        d = parse_agent_directive(stdout)
        assert d is not None
        assert d.get("isolation") == "worktree"

    def test_returns_undefined_when_required_field_is_missing(self) -> None:
        stdout = json.dumps(
            {
                "agent_directive": {
                    "subagent_type": "general-purpose",
                    # missing model
                    "description": "incomplete",
                    "prompt": "do the thing",
                },
            }
        )
        assert parse_agent_directive(stdout) is None

    def test_returns_undefined_for_non_json_stdout(self) -> None:
        assert parse_agent_directive("not json") is None
        assert parse_agent_directive("") is None
        assert parse_agent_directive(None) is None


@pytest.mark.skipif(_NODE_PATH is None, reason=_NODE_REASON)
class TestRunHooksPreStartWithAgentDirective:
    def test_captures_directive_from_a_pre_start_hook_that_emits_one(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_cmd = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-pre-start.mjs")
        Path(script_path).write_text(
            "process.stdout.write(JSON.stringify({agent_directive:"
            "{subagent_type:'general-purpose',model:'claude-opus-4-6',"
            "description:'curation phase',prompt:'do curation'}}))\n",
            encoding="utf-8",
        )
        os.chmod(script_path, 0o755 | stat.S_IRUSR)

        hooks = [
            HookConfig(
                trigger="pre_start",
                phase_filter="*",
                command=node_cmd,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        results = run_hooks(
            hooks,
            "pre_start",
            {
                "run_id": "test-run",
                "phase": "curation",
                "trigger": "pre_start",
                "project_root": hook_root,
                "run_dir": temp_dir,
            },
            hook_root,
        )

        assert len(results) == 1
        assert results[0].get("agent_directive")
        d = results[0]["agent_directive"]
        assert d["model"] == "claude-opus-4-6"
        assert d["subagent_type"] == "general-purpose"

    def test_omits_agent_directive_on_hook_result_when_hook_emits_plain_text(
        self, tmp_path: Path
    ) -> None:
        temp_dir = str(tmp_path)
        hook_root, node_cmd = _node_hook_command()
        script_path = os.path.join(temp_dir, "hook-plain.mjs")
        Path(script_path).write_text(
            "process.stdout.write('plain-text-no-directive')\n",
            encoding="utf-8",
        )
        os.chmod(script_path, 0o755 | stat.S_IRUSR)

        hooks = [
            HookConfig(
                trigger="pre_start",
                phase_filter="*",
                command=node_cmd,
                args=[script_path],
                timeout_ms=10000,
            )
        ]

        results = run_hooks(
            hooks,
            "pre_start",
            {
                "run_id": "test-run",
                "phase": "curation",
                "trigger": "pre_start",
                "project_root": hook_root,
                "run_dir": temp_dir,
            },
            hook_root,
        )

        assert len(results) == 1
        assert results[0].get("agent_directive") is None
