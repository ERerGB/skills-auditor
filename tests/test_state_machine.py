"""Tests for state_machine module: states, transitions, traces, audit."""

import json
import tempfile
import unittest
from pathlib import Path

from skills_auditor.state_machine import (
    AuditFinding,
    ClassifySignal,
    RunTrace,
    SkillIdentityTrace,
    StateTransition,
    TERMINAL_STATES,
    TRANSITIONS,
    VariantState,
    audit_traces,
    is_valid_transition,
    load_traces,
    write_trace,
)


class TestTransitions(unittest.TestCase):
    def test_valid_discovered_to_true_duplicate(self) -> None:
        self.assertTrue(is_valid_transition(VariantState.DISCOVERED, VariantState.TRUE_DUPLICATE))

    def test_valid_discovered_to_variant_detected(self) -> None:
        self.assertTrue(is_valid_transition(VariantState.DISCOVERED, VariantState.VARIANT_DETECTED))

    def test_invalid_discovered_to_active(self) -> None:
        self.assertFalse(is_valid_transition(VariantState.DISCOVERED, VariantState.ACTIVE))

    def test_terminal_states_have_no_outgoing(self) -> None:
        for state in TERMINAL_STATES:
            self.assertEqual(TRANSITIONS[state], [], f"{state} should have no outgoing transitions")

    def test_all_states_in_transitions(self) -> None:
        for state in VariantState:
            self.assertIn(state, TRANSITIONS, f"{state} missing from TRANSITIONS")

    def test_selected_to_active(self) -> None:
        self.assertTrue(is_valid_transition(VariantState.SELECTED, VariantState.ACTIVE))

    def test_superseded_to_archived(self) -> None:
        self.assertTrue(is_valid_transition(VariantState.SUPERSEDED, VariantState.ARCHIVED))

    def test_superseded_to_deleted(self) -> None:
        self.assertTrue(is_valid_transition(VariantState.SUPERSEDED, VariantState.DELETED))

    def test_unroutable_to_flagged(self) -> None:
        self.assertTrue(is_valid_transition(VariantState.UNROUTABLE, VariantState.FLAGGED))


class TestStateTransitionCreate(unittest.TestCase):
    def test_valid_transition_creates(self) -> None:
        t = StateTransition.create(
            "/a/SKILL.md",
            VariantState.DISCOVERED,
            VariantState.TRUE_DUPLICATE,
            reason="same hash",
        )
        self.assertEqual(t.from_state, "discovered")
        self.assertEqual(t.to_state, "true_duplicate")

    def test_invalid_transition_raises(self) -> None:
        with self.assertRaises(ValueError):
            StateTransition.create(
                "/a/SKILL.md",
                VariantState.DISCOVERED,
                VariantState.ACTIVE,
            )

    def test_signal_stored(self) -> None:
        t = StateTransition.create(
            "/a/SKILL.md",
            VariantState.VARIANT_DETECTED,
            VariantState.CLASSIFIED,
            signal=ClassifySignal.PATH_CONVENTION,
            inferred_platform="codex",
        )
        self.assertEqual(t.signal, "path_convention")
        self.assertEqual(t.inferred_platform, "codex")


class TestSkillIdentityTrace(unittest.TestCase):
    def test_terminal_state_for(self) -> None:
        ident = SkillIdentityTrace(skill_name="x", bundle="b", active_platform="cursor")
        ident.add_transition(StateTransition.create(
            "/a", VariantState.DISCOVERED, VariantState.TRUE_DUPLICATE,
        ))
        ident.add_transition(StateTransition.create(
            "/a", VariantState.TRUE_DUPLICATE, VariantState.SELECTED,
        ))
        ident.add_transition(StateTransition.create(
            "/a", VariantState.SELECTED, VariantState.ACTIVE,
        ))
        self.assertEqual(ident.terminal_state_for("/a"), "active")

    def test_terminal_state_none_for_unknown(self) -> None:
        ident = SkillIdentityTrace(skill_name="x", bundle="b", active_platform="cursor")
        self.assertIsNone(ident.terminal_state_for("/unknown"))


class TestTraceIO(unittest.TestCase):
    def test_write_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            trace_dir = Path(td)
            trace = RunTrace(
                skills_dir="/test",
                active_platform="cursor",
                resolve_strategy="archive",
            )
            ident = SkillIdentityTrace(
                skill_name="gstack", bundle="gstack",
                active_platform="cursor",
                variants=["/a/SKILL.md", "/b/SKILL.md"],
                final_selected="/a/SKILL.md",
                final_superseded=["/b/SKILL.md"],
            )
            ident.add_transition(StateTransition.create(
                "/a/SKILL.md", VariantState.DISCOVERED, VariantState.TRUE_DUPLICATE,
            ))
            trace.identities.append(ident)

            out = write_trace(trace, trace_dir)
            self.assertTrue(out.exists())

            loaded = load_traces(trace_dir)
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0].active_platform, "cursor")
            self.assertEqual(len(loaded[0].identities), 1)
            self.assertEqual(loaded[0].identities[0].skill_name, "gstack")
            self.assertEqual(len(loaded[0].identities[0].transitions), 1)

    def test_load_empty_dir(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            loaded = load_traces(Path(td))
            self.assertEqual(loaded, [])

    def test_load_nonexistent_dir(self) -> None:
        loaded = load_traces(Path("/nonexistent/path"))
        self.assertEqual(loaded, [])


class TestAuditTraces(unittest.TestCase):
    def _make_valid_trace(self) -> RunTrace:
        trace = RunTrace(skills_dir="/test", active_platform="cursor", resolve_strategy="archive")
        ident = SkillIdentityTrace(
            skill_name="x", bundle="b", active_platform="cursor",
            variants=["/a/SKILL.md", "/b/SKILL.md"],
        )
        ident.add_transition(StateTransition.create(
            "/a/SKILL.md", VariantState.DISCOVERED, VariantState.TRUE_DUPLICATE,
        ))
        ident.add_transition(StateTransition.create(
            "/a/SKILL.md", VariantState.TRUE_DUPLICATE, VariantState.SELECTED,
        ))
        ident.add_transition(StateTransition.create(
            "/a/SKILL.md", VariantState.SELECTED, VariantState.ACTIVE,
        ))
        ident.add_transition(StateTransition.create(
            "/b/SKILL.md", VariantState.DISCOVERED, VariantState.TRUE_DUPLICATE,
        ))
        ident.add_transition(StateTransition.create(
            "/b/SKILL.md", VariantState.TRUE_DUPLICATE, VariantState.SUPERSEDED,
        ))
        ident.add_transition(StateTransition.create(
            "/b/SKILL.md", VariantState.SUPERSEDED, VariantState.ARCHIVED,
        ))
        ident.final_selected = "/a/SKILL.md"
        ident.final_superseded = ["/b/SKILL.md"]
        trace.identities.append(ident)
        return trace

    def test_valid_trace_no_errors(self) -> None:
        trace = self._make_valid_trace()
        findings = audit_traces([trace])
        errors = [f for f in findings if f.severity == "error"]
        self.assertEqual(len(errors), 0)

    def test_illegal_transition_detected(self) -> None:
        trace = RunTrace(skills_dir="/test", active_platform="cursor")
        ident = SkillIdentityTrace(
            skill_name="x", bundle="b", active_platform="cursor",
            variants=["/a/SKILL.md"],
        )
        # Manually insert an illegal transition (bypass validation)
        ident.transitions.append(StateTransition(
            variant_path="/a/SKILL.md",
            from_state="discovered", to_state="active",
        ))
        trace.identities.append(ident)
        findings = audit_traces([trace])
        illegals = [f for f in findings if f.check == "illegal_transition"]
        self.assertTrue(len(illegals) > 0)

    def test_unused_states_reported(self) -> None:
        trace = self._make_valid_trace()
        findings = audit_traces([trace])
        unused = [f for f in findings if f.check == "unused_states"]
        self.assertTrue(len(unused) > 0, "Should report states not covered in trace")

    def test_cross_run_inconsistency(self) -> None:
        t1 = RunTrace(run_id="run1", skills_dir="/test", active_platform="cursor")
        t2 = RunTrace(run_id="run2", skills_dir="/test", active_platform="cursor")
        i1 = SkillIdentityTrace(
            skill_name="x", bundle="b", active_platform="cursor",
            variants=["/a/SKILL.md"], final_selected="/a/SKILL.md",
        )
        i2 = SkillIdentityTrace(
            skill_name="x", bundle="b", active_platform="cursor",
            variants=["/a/SKILL.md"], final_selected="/b/SKILL.md",
        )
        t1.identities.append(i1)
        t2.identities.append(i2)
        findings = audit_traces([t1, t2])
        inconsistent = [f for f in findings if f.check == "cross_run_inconsistency"]
        self.assertEqual(len(inconsistent), 1)


if __name__ == "__main__":
    unittest.main()
