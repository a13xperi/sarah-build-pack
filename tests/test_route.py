#!/usr/bin/env python3
"""Unit tests for lib/route.py — the Pillar 3 token-matrix router.

Run:  python3 -m unittest tests.test_route   (from repo root)
  or: python3 tests/test_route.py
No third-party deps.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import route  # noqa: E402


def engine(desc, **kw):
    return route.classify_task(desc, **kw)["engine"]


class TestRoutingTable(unittest.TestCase):
    # ── Opus: high blast-radius design ──
    def test_architecture_to_opus(self):
        self.assertEqual(engine("design the API architecture for billing"), "opus")

    def test_data_model_to_opus(self):
        self.assertEqual(engine("change the data model and run a migration"), "opus")

    def test_auth_to_opus(self):
        self.assertEqual(engine("rework the oauth authentication flow"), "opus")

    # ── Opus audit override (never delegated) ──
    def test_audit_override_to_opus(self):
        self.assertEqual(engine("audit the payment code before merge"), "opus")

    def test_security_review_to_opus(self):
        self.assertEqual(engine("security review of the upload endpoint"), "opus")

    # ── Codex: isolated PR / multi-file refactor ──
    def test_refactor_to_codex(self):
        self.assertEqual(engine("refactor the parser into multiple files"), "codex")

    def test_codemod_to_codex(self):
        self.assertEqual(engine("run a codemod to restructure imports"), "codex")

    # ── MiniMax: tests / boilerplate / lint ──
    def test_tests_to_minimax(self):
        self.assertEqual(engine("write unit tests for the parser"), "minimax")

    def test_lint_to_minimax(self):
        self.assertEqual(engine("fix lint and add type annotations"), "minimax")

    # ── Gemini: single-file bug / research / docs ──
    def test_bug_to_gemini(self):
        self.assertEqual(engine("fix a single-file bug in the date parser"), "gemini")

    def test_research_to_gemini(self):
        self.assertEqual(engine("research the best rate-limit algorithm"), "gemini")

    def test_docs_to_gemini(self):
        self.assertEqual(engine("update the README documentation"), "gemini")

    # ── Sonnet: director / ambiguous ──
    def test_ambiguous_to_sonnet(self):
        self.assertEqual(engine("help me think through this and decompose it"), "sonnet")

    def test_empty_to_sonnet(self):
        self.assertEqual(engine(""), "sonnet")


class TestOverlapsAndPrecedence(unittest.TestCase):
    def test_tests_for_auth_stays_cheap(self):
        # Writing tests around auth is still cheap test work, not architecture.
        self.assertEqual(engine("add unit tests for the auth module"), "minimax")

    def test_redesign_auth_is_opus(self):
        self.assertEqual(engine("redesign the auth token architecture"), "opus")

    def test_audit_beats_everything(self):
        self.assertEqual(engine("audit and refactor the billing tests"), "opus")


class TestExplicitTypeAndRisk(unittest.TestCase):
    def test_explicit_type_refactor(self):
        self.assertEqual(engine("do the thing", task_type="refactor"), "codex")

    def test_explicit_type_test(self):
        self.assertEqual(engine("do the thing", task_type="test"), "minimax")

    def test_explicit_type_audit_is_opus(self):
        self.assertEqual(engine("do the thing", task_type="audit"), "opus")

    def test_high_risk_escalates_cheap_type(self):
        # type=bug would be gemini, but risk=high escalates to opus.
        self.assertEqual(engine("touch the payment path", task_type="bug", risk="high"), "opus")

    def test_high_risk_keeps_tests_cheap(self):
        self.assertEqual(engine("write unit tests for billing", risk="high"), "minimax")

    def test_high_risk_freetext_to_opus(self):
        self.assertEqual(engine("change something important", risk="high"), "opus")


class TestResultShape(unittest.TestCase):
    def test_in_session_flags(self):
        r = route.classify_task("design the architecture")
        self.assertTrue(r["in_session"])
        self.assertIsNone(r["dispatch"])

    def test_external_dispatch_mapping(self):
        r = route.classify_task("write unit tests")
        self.assertFalse(r["in_session"])
        self.assertEqual(r["dispatch"], "mm")  # minimax → mm flag

    def test_confidence_bounds(self):
        for desc in ["", "refactor things", "audit it", "research foo"]:
            c = route.classify_task(desc)["confidence"]
            self.assertGreaterEqual(c, 0.0)
            self.assertLessEqual(c, 1.0)

    def test_engine_always_known(self):
        for desc in ["", "xyzzy", "do work", "fix it", "refactor"]:
            self.assertIn(route.classify_task(desc)["engine"], route.ALL_ENGINES)


if __name__ == "__main__":
    unittest.main(verbosity=2)
