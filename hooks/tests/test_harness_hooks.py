"""Pytest port of the Node ``run-tests.mjs`` harness (44 cases, 12 sections).

Each class corresponds to one legacy ``=== Section ===`` block; method counts
match the legacy ``test(...)`` calls exactly. The legacy assertions translate
as follows:

  * ``assertEqual(actual, expected)`` -> ``assert actual == expected``
  * ``assertIncludes(str, sub)``      -> ``assert sub in str``
  * ``assertTruthy(val)``             -> ``assert val``

Every hook invocation passes ``env=hook_env`` so the suite is hermetic; tests
that depend on runtime-config write it to ``hook_env["PIPELINE_RUNTIME_CONFIG"]``
first. Run states are seeded under ``PIPELINE_MCP_DATA_DIR`` with a future
``updated_at`` so ``find_latest_run_state`` selects them deterministically.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from hooks.tests.conftest import (
    FIXTURES_DIR,
    HOOKS_DIR,
    SCRIPTS_DIR,
    run_hook,
    seed_run_state,
    write_runtime_config,
)

# ── Script filenames (Python, underscore form) ──────────────────
ALL_SCRIPTS: tuple[str, ...] = (
    "session_init.py",
    "send_message_guard.py",
    "token_budget_guard.py",
    "phase_start_guard.py",
    "dag_guard.py",
    "artifact_gate.py",
    "file_read_version_guard.py",
    "post_phase_complete.py",
    "phase_context_inject.py",
    "phase_timing.py",
    "stop_guard.py",
)

ALL_FIXTURES: tuple[str, ...] = (
    "artifact-gate.json",
    "dag-guard-skipped-dep.json",
    "dag-guard.json",
    "file-read-version.json",
    "phase-context-inject.json",
    "phase-start.json",
    "phase-timing.json",
    "post-phase-complete.json",
    "send-message-allow.json",
    "session-start.json",
    "stop-guard.json",
    "token-budget.json",
)


def _phase_state(name: str, status: str, **extra: Any) -> dict[str, Any]:
    """Build a phase entry for a seeded run-state ``phases`` map."""
    state: dict[str, Any] = {
        "phase_name": name,
        "status": status,
        "input_artifacts": [],
        "output_artifacts": [],
        "retry_count": 0,
    }
    state.update(extra)
    return state


# ── 1. Session Init (session_init.py) — 3 ───────────────────────


class TestSessionInit:
    def test_exits_cleanly_with_no_runs_directory(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook("session_init.py", "session-start.json", env=hook_env)
        assert result.exit_code == 0

    def test_produces_system_message_when_active_run_exists(
        self, hook_env: dict[str, str]
    ) -> None:
        seed_run_state(
            hook_env["PIPELINE_MCP_DATA_DIR"],
            "active-run",
            {
                "run_id": "active-run",
                "status": "running",
                "phases": {"curation": _phase_state("curation", "in_progress")},
                "available_artifacts": [],
            },
        )
        result = run_hook("session_init.py", "session-start.json", env=hook_env)
        assert result.exit_code == 0
        if result.parsed and result.parsed.get("systemMessage"):
            assert "pipeline" in result.parsed["systemMessage"]

    def test_system_message_has_no_naive_next_available_line(
        self, hook_env: dict[str, str]
    ) -> None:
        seed_run_state(
            hook_env["PIPELINE_MCP_DATA_DIR"],
            "active-run-2",
            {
                "run_id": "active-run-2",
                "status": "running",
                "phases": {"curation": _phase_state("curation", "in_progress")},
                "available_artifacts": [],
            },
        )
        result = run_hook("session_init.py", "session-start.json", env=hook_env)
        assert result.exit_code == 0
        if result.parsed and result.parsed.get("systemMessage"):
            msg: str = result.parsed["systemMessage"]
            assert ("Next available:" in msg) is False
            assert "pipeline_next_phases" in msg


# ── 2. Send Message Guard (send_message_guard.py) — 3 ───────────


class TestSendMessageGuard:
    def test_returns_ask_when_mode_is_ask(self, hook_env: dict[str, str]) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"send_message_guard": {"enabled": True, "mode": "ask"}},
        )
        result = run_hook(
            "send_message_guard.py", "send-message-allow.json", env=hook_env
        )
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "ask"

    def test_returns_allow_when_mode_is_allow(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"send_message_guard": {"enabled": True, "mode": "allow"}},
        )
        result = run_hook(
            "send_message_guard.py", "send-message-allow.json", env=hook_env
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_returns_deny_with_reason_when_mode_is_deny(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "send_message_guard": {
                    "enabled": True,
                    "mode": "deny",
                    "deny_reason": "Custom deny.",
                }
            },
        )
        result = run_hook(
            "send_message_guard.py", "send-message-allow.json", env=hook_env
        )
        assert result.parsed["permissionDecision"] == "deny"
        assert result.parsed["deny_reason"] == "Custom deny."


# ── 3. Token Budget Guard (token_budget_guard.py) — 4 ───────────


class TestTokenBudgetGuard:
    def test_allows_when_no_transcript_path(self, hook_env: dict[str, str]) -> None:
        write_runtime_config(hook_env["PIPELINE_RUNTIME_CONFIG"])
        result = run_hook("token_budget_guard.py", "token-budget.json", env=hook_env)
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "allow"

    def test_allows_when_cost_within_budget(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"token_budget_guard": {"max_extra_usage_usd": 100.0, "warn_at_pct": 80}},
        )
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                    "model": "claude-sonnet-4-20250514",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        payload = json.loads(
            (FIXTURES_DIR / "token-budget.json").read_text(encoding="utf-8")
        )
        payload["transcript_path"] = str(transcript)
        result = run_hook("token_budget_guard.py", payload, env=hook_env)
        assert result.parsed["permissionDecision"] == "allow"

    def test_denies_when_cost_exceeds_budget(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"token_budget_guard": {"max_extra_usage_usd": 0.0001, "warn_at_pct": 80}},
        )
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "usage": {"input_tokens": 100000, "output_tokens": 50000},
                    "model": "claude-opus-4-20250514",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        payload = json.loads(
            (FIXTURES_DIR / "token-budget.json").read_text(encoding="utf-8")
        )
        payload["transcript_path"] = str(transcript)
        result = run_hook("token_budget_guard.py", payload, env=hook_env)
        assert result.parsed["permissionDecision"] == "deny"
        assert "budget" in result.parsed["deny_reason"]

    def test_coerces_string_token_counts_without_bypass(
        self, hook_env: dict[str, str], tmp_path: Path
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"token_budget_guard": {"max_extra_usage_usd": 0.0001, "warn_at_pct": 80}},
        )
        # Attacker-supplied string token counts. safe_number() coerces these to
        # finite floats; the budget check must still fire (deny), not bypass.
        # cache_read_input_tokens is OMITTED entirely (mirrors the JS
        # `undefined` field); cache_creation_input_tokens is null.
        transcript = tmp_path / "transcript.jsonl"
        transcript.write_text(
            json.dumps(
                {
                    "usage": {
                        "input_tokens": "99999",
                        "output_tokens": "50000",
                        "cache_creation_input_tokens": None,
                    },
                    "model": "claude-opus-4-20250514",
                }
            )
            + "\n",
            encoding="utf-8",
        )
        payload = json.loads(
            (FIXTURES_DIR / "token-budget.json").read_text(encoding="utf-8")
        )
        payload["transcript_path"] = str(transcript)
        result = run_hook("token_budget_guard.py", payload, env=hook_env)
        assert result.parsed["permissionDecision"] == "deny"


# ── 4. Phase Start Guard (phase_start_guard.py) — 7 ─────────────


class TestPhaseStartGuard:
    def test_allows_first_phase_start(self, hook_env: dict[str, str]) -> None:
        write_runtime_config(hook_env["PIPELINE_RUNTIME_CONFIG"])
        result = run_hook(
            "phase_start_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "test_unique_phase"},
                "session_id": f"test-unique-{time.time_ns()}",
            },
            env=hook_env,
        )
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "allow"

    def test_warns_on_repeat_start_in_warn_only_mode(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"phase_session_isolation": {"enabled": True, "warn_only": True}},
        )
        payload: dict[str, Any] = {
            "event": "PreToolUse",
            "tool_name": "mcp__pipeline__pipeline_start_phase",
            "tool_input": {"phase": "repeat_test_phase"},
            "session_id": f"test-repeat-{time.time_ns()}",
        }
        run_hook("phase_start_guard.py", payload, env=hook_env)
        result = run_hook("phase_start_guard.py", payload, env=hook_env)
        assert result.parsed["permissionDecision"] == "allow"
        assert "already started" in result.parsed["systemMessage"]

    def test_denies_on_repeat_start_when_warn_only_false(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {"phase_session_isolation": {"enabled": True, "warn_only": False}},
        )
        payload: dict[str, Any] = {
            "event": "PreToolUse",
            "tool_name": "mcp__pipeline__pipeline_start_phase",
            "tool_input": {"phase": "deny_test_phase"},
            "session_id": f"test-deny-{time.time_ns()}",
        }
        run_hook("phase_start_guard.py", payload, env=hook_env)
        result = run_hook("phase_start_guard.py", payload, env=hook_env)
        assert result.parsed["permissionDecision"] == "deny"
        assert "already started" in result.parsed["deny_reason"]

    def test_denies_path_traversal_session_id(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(hook_env["PIPELINE_RUNTIME_CONFIG"])
        result = run_hook(
            "phase_start_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "traversal_test"},
                "session_id": "../../../etc/passwd",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "deny"
        assert "unsafe session_id" in result.parsed["deny_reason"]

    def test_denies_session_id_exceeding_length(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(hook_env["PIPELINE_RUNTIME_CONFIG"])
        result = run_hook(
            "phase_start_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "long_test"},
                "session_id": "a" * 200,
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "deny"
        assert "exceeds" in result.parsed["deny_reason"]

    def test_allows_safe_alphanumeric_session_id(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(hook_env["PIPELINE_RUNTIME_CONFIG"])
        result = run_hook(
            "phase_start_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": f"safe_unique_{time.time_ns()}"},
                "session_id": f"safe-session-{time.time_ns()}",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_cleans_up_stale_session_files_older_than_24h(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(hook_env["PIPELINE_RUNTIME_CONFIG"])
        track_dir = Path(hook_env["TMPDIR"]) / "pipeline-hooks-sessions"
        track_dir.mkdir(parents=True, exist_ok=True)

        stale_one = track_dir / "session-stale-one.json"
        stale_two = track_dir / "session-stale-two.json"
        fresh = track_dir / "session-fresh.json"
        stale_one.write_text('{"phases_started":["a"]}', encoding="utf-8")
        stale_two.write_text('{"phases_started":["b"]}', encoding="utf-8")
        fresh.write_text('{"phases_started":["c"]}', encoding="utf-8")

        # Backdate the two stale files to 25h ago (mtime/atime in seconds).
        stale_time = time.time() - 25 * 60 * 60
        os.utime(stale_one, (stale_time, stale_time))
        os.utime(stale_two, (stale_time, stale_time))

        run_hook(
            "phase_start_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": f"ttl_test_{time.time_ns()}"},
                "session_id": f"ttl-session-{time.time_ns()}",
            },
            env=hook_env,
        )

        assert stale_one.exists() is False
        assert stale_two.exists() is False
        assert fresh.exists() is True


# ── 5. DAG Guard (dag_guard.py) — 5 ─────────────────────────────


class TestDagGuard:
    def test_allows_entry_point_with_no_required_deps(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook(
            "dag_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "curation"},
                "session_id": "test-session-001",
            },
            env=hook_env,
        )
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "allow"

    def test_allows_phase_with_no_phase_name(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook(
            "dag_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {},
                "session_id": "test-session-001",
            },
            env=hook_env,
        )
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "allow"

    def test_allows_phase_whose_required_dep_is_skipped(
        self, hook_env: dict[str, str]
    ) -> None:
        seed_run_state(
            hook_env["PIPELINE_MCP_DATA_DIR"],
            "test-skipped-dep",
            {
                "run_id": "test-skipped-dep",
                "pipeline_version": "1.0.0",
                "status": "running",
                "phases": {
                    "curation": _phase_state("curation", "skipped"),
                    "design_synthesis": _phase_state("design_synthesis", "pending"),
                },
                "available_artifacts": [],
                "config_path": "",
            },
        )
        result = run_hook(
            "dag_guard.py", "dag-guard-skipped-dep.json", env=hook_env
        )
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "allow"

    def test_allows_phase_whose_required_dep_is_present(
        self, hook_env: dict[str, str]
    ) -> None:
        seed_run_state(
            hook_env["PIPELINE_MCP_DATA_DIR"],
            "test-pending-dep",
            {
                "run_id": "test-pending-dep",
                "pipeline_version": "1.0.0",
                "status": "running",
                "phases": {
                    "design_synthesis": _phase_state(
                        "design_synthesis", "completed"
                    ),
                    "debate": _phase_state("debate", "pending"),
                    "validation": _phase_state("validation", "pending"),
                },
                "available_artifacts": [],
                "config_path": "",
            },
        )
        result = run_hook(
            "dag_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "validation"},
                "session_id": "test-pending-dep",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_failed_dep_produces_structural_json(
        self, hook_env: dict[str, str]
    ) -> None:
        # validation is entry_point=true in pipeline.toml, so dag_guard allows
        # it regardless of dep status — this is a structural-output guard only.
        seed_run_state(
            hook_env["PIPELINE_MCP_DATA_DIR"],
            "test-failed-dep",
            {
                "run_id": "test-failed-dep",
                "pipeline_version": "1.0.0",
                "status": "running",
                "phases": {
                    "design_synthesis": _phase_state(
                        "design_synthesis", "failed", error="synthesis failed"
                    ),
                    "validation": _phase_state("validation", "pending"),
                },
                "available_artifacts": [],
                "config_path": "",
            },
        )
        result = run_hook(
            "dag_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "validation"},
                "session_id": "test-failed-dep",
            },
            env=hook_env,
        )
        assert result.exit_code == 0
        assert result.parsed


# ── 6. Artifact Gate (artifact_gate.py) — 3 ─────────────────────


class TestArtifactGate:
    def test_exits_cleanly_and_produces_valid_json(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook("artifact_gate.py", "artifact-gate.json", env=hook_env)
        assert result.exit_code == 0
        assert result.parsed
        assert result.parsed["permissionDecision"]

    def test_allows_when_no_phase_name(self, hook_env: dict[str, str]) -> None:
        result = run_hook(
            "artifact_gate.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_complete_phase",
                "tool_input": {},
                "session_id": "test",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_denies_completion_when_phase_has_zero_artifacts(
        self, hook_env: dict[str, str]
    ) -> None:
        seed_run_state(
            hook_env["PIPELINE_MCP_DATA_DIR"],
            "test-no-artifacts",
            {
                "run_id": "test-no-artifacts",
                "pipeline_version": "1.0.0",
                "status": "running",
                "phases": {
                    "curation": _phase_state("curation", "in_progress"),
                },
                "available_artifacts": [],
                "config_path": "",
            },
        )
        result = run_hook(
            "artifact_gate.py",
            {
                "event": "PreToolUse",
                "tool_name": "mcp__pipeline__pipeline_complete_phase",
                "tool_input": {"phase": "curation"},
                "session_id": "test-no-artifacts",
            },
            env=hook_env,
        )
        assert result.exit_code == 0
        assert result.parsed["permissionDecision"] == "deny"
        assert "no artifacts" in result.parsed["deny_reason"]


# ── 7. File Read Version Guard (file_read_version_guard.py) — 6 ──


class TestFileReadVersionGuard:
    def test_denies_outdated_version_tag(self, hook_env: dict[str, str]) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "file_read_version_guard": {
                    "enabled": True,
                    "present_version_in_development": "v0.4.0",
                    "previous_versions": ["v0.3.0", "v0.2.0"],
                }
            },
        )
        result = run_hook(
            "file_read_version_guard.py", "file-read-version.json", env=hook_env
        )
        assert result.parsed["permissionDecision"] == "deny"
        assert "outdated" in result.parsed["deny_reason"]

    def test_allows_current_version(self, hook_env: dict[str, str]) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "file_read_version_guard": {
                    "enabled": True,
                    "present_version_in_development": "v0.4.0",
                    "previous_versions": ["v0.3.0"],
                }
            },
        )
        result = run_hook(
            "file_read_version_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/project/specs/v0.4.0-design.json"},
                "session_id": "test",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_allows_unversioned_files(self, hook_env: dict[str, str]) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "file_read_version_guard": {
                    "enabled": True,
                    "present_version_in_development": "v0.4.0",
                    "previous_versions": ["v0.3.0"],
                }
            },
        )
        result = run_hook(
            "file_read_version_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/project/README.md"},
                "session_id": "test",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_allows_when_no_version_configured(
        self, hook_env: dict[str, str]
    ) -> None:
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "file_read_version_guard": {
                    "enabled": True,
                    "present_version_in_development": "",
                    "previous_versions": [],
                }
            },
        )
        result = run_hook(
            "file_read_version_guard.py", "file-read-version.json", env=hook_env
        )
        assert result.parsed["permissionDecision"] == "allow"

    def test_denies_version_bypass_path_after_normalization(
        self, hook_env: dict[str, str]
    ) -> None:
        # `/project/v0.4.0/../v0.3.0/specs.json` normalizes to a path under
        # v0.3.0 (the `v0.4.0/..` segment collapses), so the matcher only sees
        # the outdated version and denies. Locks the substring-bypass closed.
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "file_read_version_guard": {
                    "enabled": True,
                    "present_version_in_development": "v0.4.0",
                    "previous_versions": ["v0.3.0", "v0.2.0"],
                }
            },
        )
        result = run_hook(
            "file_read_version_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "/project/v0.4.0/../v0.3.0/specs.json"},
                "session_id": "test",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "deny"
        assert "v0.3.0" in result.parsed["deny_reason"]

    def test_denies_crafted_relative_path_with_unresolved_dotdot(
        self, hook_env: dict[str, str]
    ) -> None:
        # `docs/../../../etc/passwd` retains unresolved `..` after normpath on
        # both posix and win32 — the normalization-deny branch must fire.
        write_runtime_config(
            hook_env["PIPELINE_RUNTIME_CONFIG"],
            {
                "file_read_version_guard": {
                    "enabled": True,
                    "present_version_in_development": "v0.4.0",
                    "previous_versions": ["v0.3.0"],
                }
            },
        )
        result = run_hook(
            "file_read_version_guard.py",
            {
                "event": "PreToolUse",
                "tool_name": "Read",
                "tool_input": {"file_path": "docs/../../../etc/passwd"},
                "session_id": "test",
            },
            env=hook_env,
        )
        assert result.parsed["permissionDecision"] == "deny"
        assert "normalization" in result.parsed["deny_reason"]


# ── 8. Post Phase Complete (post_phase_complete.py) — 1 ─────────


class TestPostPhaseComplete:
    def test_produces_system_message_on_completion(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook(
            "post_phase_complete.py", "post-phase-complete.json", env=hook_env
        )
        assert result.exit_code == 0
        if result.parsed:
            assert result.parsed["systemMessage"]
            assert "curation" in result.parsed["systemMessage"]


# ── 9. Phase Context Inject (phase_context_inject.py) — 2 ───────


class TestPhaseContextInject:
    def test_injects_context_for_known_phase(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook(
            "phase_context_inject.py", "phase-context-inject.json", env=hook_env
        )
        assert result.exit_code == 0
        if result.parsed:
            assert result.parsed["systemMessage"]
            assert "curation" in result.parsed["systemMessage"]

    def test_exits_cleanly_for_unknown_phase(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook(
            "phase_context_inject.py",
            {
                "event": "PostToolUse",
                "tool_name": "mcp__pipeline__pipeline_start_phase",
                "tool_input": {"phase": "nonexistent_phase_xyz"},
                "session_id": "test",
            },
            env=hook_env,
        )
        assert result.exit_code == 0


# ── 10. Phase Timing (phase_timing.py) — 1 ──────────────────────


class TestPhaseTiming:
    def test_writes_timing_entry_and_exits_cleanly(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook("phase_timing.py", "phase-timing.json", env=hook_env)
        assert result.exit_code == 0

        timing_path = Path(hook_env["PIPELINE_HOOK_LOGS_DIR"]) / "timing.jsonl"
        assert timing_path.exists()
        lines = timing_path.read_text(encoding="utf-8").strip().split("\n")
        entry: dict[str, Any] = json.loads(lines[-1])
        assert entry["phase"] == "curation"
        assert entry["event"] == "phase_started"
        assert entry["timestamp"]
        assert entry["session_id"] == "test-session-003"


# ── 11. Stop Guard (stop_guard.py) — 2 ──────────────────────────


class TestStopGuard:
    def test_exits_cleanly_when_no_active_run(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook("stop_guard.py", "stop-guard.json", env=hook_env)
        assert result.exit_code == 0

    def test_exits_cleanly_when_stop_hook_active(
        self, hook_env: dict[str, str]
    ) -> None:
        result = run_hook(
            "stop_guard.py",
            {"event": "Stop", "session_id": "test", "stop_hook_active": True},
            env=hook_env,
        )
        assert result.exit_code == 0


# ── 12. Structural Validation — 7 ───────────────────────────────


class TestStructural:
    def test_all_eleven_hook_scripts_exist(self) -> None:
        for script in ALL_SCRIPTS:
            assert (SCRIPTS_DIR / script).exists(), f"{script} missing"
        assert len(ALL_SCRIPTS) == 11

    def test_all_scripts_start_with_shebang(self) -> None:
        for script in ALL_SCRIPTS:
            content = (SCRIPTS_DIR / script).read_text(encoding="utf-8")
            assert content.startswith("#!/usr/bin/env python3"), f"{script} shebang"

    def test_all_fixtures_are_valid_json(self) -> None:
        for fixture in ALL_FIXTURES:
            content = (FIXTURES_DIR / fixture).read_text(encoding="utf-8")
            json.loads(content)  # raises on invalid JSON
        assert len(ALL_FIXTURES) == 12

    def test_hooks_config_toml_exists(self) -> None:
        assert (HOOKS_DIR / "hooks-config.toml").exists()

    def test_generate_settings_exists(self) -> None:
        assert (HOOKS_DIR / "generate_settings.py").exists()

    def test_generate_settings_emits_double_quoted_command_paths(
        self, tmp_path: Path
    ) -> None:
        runtime_config = tmp_path / "runtime-config.json"
        settings_path = tmp_path / "settings.local.json"
        env: dict[str, str] = {
            **os.environ,
            "PIPELINE_HOOKS_CONFIG": str(HOOKS_DIR / "hooks-config.toml"),
            "PIPELINE_RUNTIME_CONFIG": str(runtime_config),
            "PIPELINE_SETTINGS_PATH": str(settings_path),
        }
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, str(HOOKS_DIR / "generate_settings.py")],
            capture_output=True,
            text=True,
            timeout=15.0,
            env=env,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        assert settings_path.exists()
        parsed: Any = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks: list[dict[str, Any]] = parsed.get("hooks") or []
        assert len(hooks) > 0
        pattern = re.compile(r'^python3 "[^"]+"$')
        for entry in hooks:
            command = entry.get("command")
            assert isinstance(command, str)
            assert pattern.match(command), f"command not double-quoted: {command}"

    def test_read_stdin_fails_open_on_oversized_input(self) -> None:
        payload = "x" * (15 * 1024 * 1024)
        completed: subprocess.CompletedProcess[str] = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "dag_guard.py")],
            input=payload,
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
        assert completed.returncode == 0
        parsed: Any = json.loads(completed.stdout.strip())
        assert parsed["permissionDecision"] == "allow"
