"""Port of ``legacy-node/tests/scaffold-anchor-validation.test.ts`` (19 cases).

Byte-exact behavioral parity tests for the scaffold anchor-validation helpers +
the advisory-warning surfacing in
:func:`pipeline_orchestrator.tools.artifact_tools.handle_register_scaffold_outputs`:

* ``extractProposedDiffAnchorRefs`` x10 — the pure anchor-extraction helper.
* ``markdownHasHeading`` x6 — the pure ATX-heading matcher.
* ``handleRegisterScaffoldOutputs anchor validation`` x3 — the advisory
  ``data.anchor_warnings`` + top-level ``warnings`` surfacing (both OMITTED when
  anchors resolve / target absent).

The Node ``makeCtx`` is reproduced as a :class:`RegisterScaffoldOutputsContext`
backed by the real ``run_state`` helpers; ``base_dir_from_run_dir`` points at a
subdirectory so the project-root lookup (``resolve(baseDir, '..')``) lands inside
the harness. Fixtures use ``tmp_path``; all paths are OS-agnostic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pipeline_orchestrator.models import ArtifactRef, RunState
from pipeline_orchestrator.run_state import add_artifact, init_run
from pipeline_orchestrator.tools.artifact_tools import (
    RegisterScaffoldOutputsContext,
    extract_proposed_diff_anchor_refs,
    handle_register_scaffold_outputs,
    markdown_has_heading,
)


def make_ctx(
    base_dir: str,
    run_state: list[RunState],
    run_dir: str,
) -> RegisterScaffoldOutputsContext:
    """Build a :class:`RegisterScaffoldOutputsContext` over real run-state helpers.

    ``get_active_run`` returns ``None`` (mirroring the Node test) so the handler
    hits the legacy ``<baseDir>/scaffold`` default-dir branch; ``base_dir`` is a
    subdirectory of the root tmp dir so ``resolve(baseDir, '..')`` lands inside
    the harness.
    """

    def _set_active(state: RunState) -> None:
        run_state[0] = state

    def _add_artifact(state: RunState, ref: ArtifactRef, d: str) -> RunState:
        return add_artifact(state, ref, d)

    return RegisterScaffoldOutputsContext(
        require_run=lambda: run_state[0],
        set_active_run=_set_active,
        get_active_run=lambda: None,
        get_active_run_dir=lambda: run_dir,
        base_dir_from_run_dir=lambda _d: base_dir,
        add_artifact=_add_artifact,
    )


# ── extractProposedDiffAnchorRefs ─────────────────────────────────────


class TestExtractProposedDiffAnchorRefs:
    def test_returns_empty_for_empty_string(self) -> None:
        assert extract_proposed_diff_anchor_refs("") == []

    def test_returns_empty_for_content_with_no_anchor_reference_phrases(
        self,
    ) -> None:
        content = "\n".join(
            [
                "# Proposed Diff",
                "This file is a placeholder patch with no anchor references at all.",
                "It contains prose about testing and ## headings in body text only.",
            ]
        )
        assert extract_proposed_diff_anchor_refs(content) == []

    def test_captures_double_quoted_under_the_existing_section_reference(
        self,
    ) -> None:
        content = (
            "Apply the section below by inserting it into AGENTS.md under the "
            'existing "Quality Gates" section.'
        )
        assert extract_proposed_diff_anchor_refs(content) == ["quality gates"]

    def test_captures_single_quoted_under_the_existing_section_reference(
        self,
    ) -> None:
        content = (
            "Insert the patch under the existing 'Core Patterns' section "
            "of the file."
        )
        assert extract_proposed_diff_anchor_refs(content) == ["core patterns"]

    def test_captures_insert_after_phrasing(self) -> None:
        content = (
            'Insert after the "Architectural Constraints" section so it lands '
            "near the top."
        )
        assert extract_proposed_diff_anchor_refs(content) == [
            "architectural constraints"
        ]

    def test_captures_under_the_x_heading_phrasing_without_existing(self) -> None:
        content = (
            'Put the new note under the "Operational Notes" heading for visibility.'
        )
        assert extract_proposed_diff_anchor_refs(content) == ["operational notes"]

    def test_does_not_match_a_create_a_new_top_level_declaration(self) -> None:
        content = (
            "Create a new top-level `## Quality Gates` section positioned after "
            "`## Core Patterns`."
        )
        assert extract_proposed_diff_anchor_refs(content) == []

    def test_normalizes_backtick_quoted_atx_headings_by_stripping_hashes(
        self,
    ) -> None:
        content = (
            "Apply the diff under the existing `## Quality Gates` section of "
            "AGENTS.md."
        )
        assert extract_proposed_diff_anchor_refs(content) == ["quality gates"]

    def test_dedupes_repeated_anchor_references_across_phrasings(self) -> None:
        content = "\n".join(
            [
                'Insert it into AGENTS.md under the existing "Quality Gates" section.',
                'Ensure it lives under the existing "Quality Gates" section.',
                'Specifically: insert after the "Quality Gates" section header.',
            ]
        )
        assert extract_proposed_diff_anchor_refs(content) == ["quality gates"]

    def test_lowercases_captured_headings_for_case_insensitive_matching(
        self,
    ) -> None:
        content = 'Place it under the existing "QUALITY GATES" section, please.'
        assert extract_proposed_diff_anchor_refs(content) == ["quality gates"]


# ── markdownHasHeading ────────────────────────────────────────────────


class TestMarkdownHasHeading:
    def test_returns_true_for_exact_h2_heading_match(self) -> None:
        content = "\n".join(["# Title", "", "## Quality Gates", "", "Body."])
        assert markdown_has_heading(content, "Quality Gates") is True

    def test_returns_true_for_case_insensitive_heading_matches(self) -> None:
        content = "## Quality Gates\n\nBody text.\n"
        assert markdown_has_heading(content, "quality gates") is True
        assert markdown_has_heading(content, "QUALITY GATES") is True

    def test_returns_false_when_the_heading_is_absent(self) -> None:
        content = "## Core Patterns\n\nBody text.\n"
        assert markdown_has_heading(content, "Quality Gates") is False

    def test_returns_true_for_h1_h3_and_h6_headings(self) -> None:
        assert markdown_has_heading("# Quality Gates\n", "Quality Gates") is True
        assert markdown_has_heading("### Quality Gates\n", "Quality Gates") is True
        assert markdown_has_heading("###### Quality Gates\n", "Quality Gates") is True

    def test_returns_false_when_heading_appears_only_in_body_text(self) -> None:
        content = "\n".join(
            [
                "# Title",
                "",
                "Here we mention Quality Gates in prose, but there is no heading.",
                "Quality Gates appear inline only.",
            ]
        )
        assert markdown_has_heading(content, "Quality Gates") is False

    def test_returns_false_for_empty_content_or_empty_needle(self) -> None:
        assert markdown_has_heading("", "Quality Gates") is False
        assert markdown_has_heading("## Quality Gates\n", "") is False


# ── handleRegisterScaffoldOutputs anchor validation ───────────────────


class TestHandleRegisterScaffoldOutputsAnchorValidation:
    @staticmethod
    def _setup(tmp_path: Path) -> tuple[str, str, str, str]:
        root_dir = str(tmp_path)
        base_dir = os.path.join(root_dir, "pipeline_mcp_data")
        run_dir = os.path.join(base_dir, "runs", "scaffold-run")
        scaffold_dir = os.path.join(base_dir, "scaffold")
        os.makedirs(run_dir, exist_ok=True)
        os.makedirs(scaffold_dir, exist_ok=True)
        return root_dir, base_dir, run_dir, scaffold_dir

    def test_emits_anchor_warning_when_proposed_diff_references_missing_heading(
        self, tmp_path: Path
    ) -> None:
        root_dir, base_dir, run_dir, scaffold_dir = self._setup(tmp_path)
        with open(os.path.join(root_dir, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write(
                "\n".join(
                    [
                        "# AGENTS",
                        "",
                        "## Core Patterns",
                        "",
                        "Unrelated content — note that there is no "
                        '"Quality Gates" heading.',
                    ]
                )
            )
        with open(
            os.path.join(scaffold_dir, "AGENTS.md.proposed-diff.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(
                "\n".join(
                    [
                        "# AGENTS.md proposed diff",
                        "",
                        "Apply the section below by inserting it into `AGENTS.md` "
                        'under the existing "Quality Gates" section.',
                    ]
                )
            )

        run_state = [
            init_run(
                "scaffold-anchor-run",
                "1.0.0",
                ["implementation_scaffold"],
                run_dir,
            )
        ]
        ctx = make_ctx(base_dir, run_state, run_dir)

        result = handle_register_scaffold_outputs({}, ctx)
        assert not result.is_error

        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert len(parsed["data"]["registered"]) == 1
        assert (
            parsed["data"]["registered"][0]["filename"]
            == "AGENTS.md.proposed-diff.md"
        )
        assert len(run_state[0].available_artifacts) == 1

        assert isinstance(parsed.get("warnings"), list)
        assert len(parsed["warnings"]) >= 1
        assert any("quality gates" in w.lower() for w in parsed["warnings"])
        assert len(parsed["data"]["anchor_warnings"]) == 1
        assert (
            parsed["data"]["anchor_warnings"][0]["filename"]
            == "AGENTS.md.proposed-diff.md"
        )
        assert parsed["data"]["anchor_warnings"][0]["target_file"] == "AGENTS.md"
        assert (
            parsed["data"]["anchor_warnings"][0]["missing_anchor"] == "quality gates"
        )

    def test_emits_no_warnings_when_referenced_heading_exists(
        self, tmp_path: Path
    ) -> None:
        root_dir, base_dir, run_dir, scaffold_dir = self._setup(tmp_path)
        with open(os.path.join(root_dir, "AGENTS.md"), "w", encoding="utf-8") as fh:
            fh.write(
                "\n".join(
                    [
                        "# AGENTS",
                        "",
                        "## Quality Gates",
                        "",
                        "Existing quality gates content goes here.",
                    ]
                )
            )
        with open(
            os.path.join(scaffold_dir, "AGENTS.md.proposed-diff.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write(
                "\n".join(
                    [
                        "# AGENTS.md proposed diff",
                        "",
                        'Apply the patch under the existing "Quality Gates" section.',
                    ]
                )
            )

        run_state = [
            init_run(
                "scaffold-anchor-run-2",
                "1.0.0",
                ["implementation_scaffold"],
                run_dir,
            )
        ]
        ctx = make_ctx(base_dir, run_state, run_dir)

        result = handle_register_scaffold_outputs({}, ctx)
        assert not result.is_error

        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert len(parsed["data"]["registered"]) == 1
        assert "warnings" not in parsed
        assert "anchor_warnings" not in parsed["data"]

    def test_skips_validation_when_target_file_absent(
        self, tmp_path: Path
    ) -> None:
        _root_dir, base_dir, run_dir, scaffold_dir = self._setup(tmp_path)
        with open(
            os.path.join(scaffold_dir, "AGENTS.md.proposed-diff.md"),
            "w",
            encoding="utf-8",
        ) as fh:
            fh.write('Apply this under the existing "Some Heading" section.')

        run_state = [
            init_run(
                "scaffold-anchor-run-3",
                "1.0.0",
                ["implementation_scaffold"],
                run_dir,
            )
        ]
        ctx = make_ctx(base_dir, run_state, run_dir)

        result = handle_register_scaffold_outputs({}, ctx)
        assert not result.is_error

        parsed = json.loads(result.json)
        assert parsed["status"] == "ok"
        assert len(parsed["data"]["registered"]) == 1
        assert "warnings" not in parsed
        assert "anchor_warnings" not in parsed["data"]
