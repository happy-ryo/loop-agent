from __future__ import annotations

import sys

from loop_agent import ActOutcome, CommandVerifier, PytestVerifier, RegexVerifier


def test_regex_verifier_matches_observation_text_attribute() -> None:
    class Observation:
        text = "work complete: DONE"

    verdict = RegexVerifier(r"\bDONE\b")(ActOutcome(observation=Observation()))

    assert verdict.goal_met
    assert "regex matched" in verdict.detail


def test_regex_verifier_reports_miss() -> None:
    verdict = RegexVerifier("DONE")(ActOutcome(observation="still working"))

    assert not verdict.goal_met
    assert "did not match" in verdict.detail


def test_command_verifier_uses_exit_code() -> None:
    verifier = CommandVerifier((sys.executable, "-c", "raise SystemExit(0)"))

    assert verifier(ActOutcome()).goal_met


def test_command_verifier_reports_nonzero_exit() -> None:
    verifier = CommandVerifier((sys.executable, "-c", "raise SystemExit(7)"))

    verdict = verifier(ActOutcome())

    assert not verdict.goal_met
    assert "exit=7" in verdict.detail


def test_pytest_verifier_builds_pytest_command_with_injected_python() -> None:
    verifier = PytestVerifier(
        ["--version"],
        python=sys.executable,
        timeout=30,
    )

    verdict = verifier(ActOutcome())

    assert verdict.goal_met
    assert "pytest" in verdict.detail.lower()
