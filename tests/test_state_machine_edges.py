"""Persistence and audit edges for route traces."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from skills_auditor.state_machine import (
    RunTrace,
    SkillIdentityTrace,
    StateTransition,
    VariantState,
    audit_traces,
    load_traces,
)


class TestStateMachineEdges(unittest.TestCase):
    def test_loader_skips_malformed_trace_files(self) -> None:
        with tempfile.TemporaryDirectory() as base:
            root = Path(base)
            (root / "bad-json.json").write_text("{", encoding="utf-8")
            (root / "bad-shape.json").write_text("[]", encoding="utf-8")
            self.assertEqual(load_traces(root), [])

    def test_audit_reports_unknown_nonterminal_and_flagged_states(self) -> None:
        unknown = StateTransition(
            variant_path="unknown",
            from_state="missing",
            to_state="also-missing",
        )
        nonterminal = StateTransition.create(
            "nonterminal",
            VariantState.DISCOVERED,
            VariantState.VARIANT_DETECTED,
        )
        flagged_first = StateTransition.create(
            "flagged", VariantState.DISCOVERED, VariantState.VARIANT_DETECTED
        )
        flagged_second = StateTransition.create(
            "flagged", VariantState.VARIANT_DETECTED, VariantState.UNROUTABLE
        )
        flagged_third = StateTransition.create(
            "flagged", VariantState.UNROUTABLE, VariantState.FLAGGED
        )
        identity = SkillIdentityTrace(
            skill_name="alpha",
            bundle="bundle",
            active_platform="codex",
            variants=["unknown", "nonterminal", "flagged", "never-seen"],
            transitions=[unknown, nonterminal, flagged_first, flagged_second, flagged_third],
        )
        trace = RunTrace(run_id="run", identities=[identity])
        checks = {finding.check for finding in audit_traces([trace])}
        self.assertTrue(
            {"unknown_state", "non_terminal_final", "unroutable_frequency", "no_terminal_state"}
            <= checks
        )


if __name__ == "__main__":
    unittest.main()
