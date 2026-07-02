#!/usr/bin/env python3
"""Demo of the outer Reflexion loop: lessons from failure improve the next episode (Issue #22).

The inner ReAct loop (``run_loop``) is wrapped as one episode. At episode boundaries,
linguistic guidance is extracted from failed trajectories, admitted into episodic memory,
and wired into the next episode's context. The primary ground-truth signal (inner verify)
drives convergence, while the rubric evaluator fixed within each epoch only emits the
reward for reflection (two-signal model). The evaluator is updated only at epoch boundaries
when the candidate beats the incumbent on agreement with fixed held-out gold labels (RQGM).

CLI output is ASCII-only (to avoid --help / print crashes on cp932 terminals).

Run:

    python3 examples/reflexion_demo.py
"""

from __future__ import annotations

from loop_agent import (
    ActOutcome,
    Evaluator,
    GroundTruthSignal,
    HeldOut,
    Lesson,
    MaxEpisodes,
    MaxIterations,
    Probe,
    RubricThreshold,
    Score,
    VerifyOutcome,
    run_loop,
    run_reflexion,
)
from loop_agent.memory import step_signature

DECLARED_KEYS = ("correctness",)
LESSON_HINT = "increment the index by 1"


def make_episode():
    """Production path: one episode is one inner run_loop; memory lessons affect success."""

    def episode(ctx):
        has_lesson = LESSON_HINT in ctx.memory_block

        def act(_inner_ctx):
            obs = "off-by-one fixed" if has_lesson else "off-by-one bug remains"
            return ActOutcome(observation=obs, tokens=5)

        def verify(outcome):
            return VerifyOutcome(goal_met="fixed" in outcome.observation)

        # Inner ReAct loop. It exits naturally when verify returns goal_met.
        return run_loop(act=act, verify=verify, conditions=[MaxIterations(2)])

    return episode


def ground_truth(outcome):
    """Primary signal: derived from inner verify success (like tests/lint), not the evaluator."""
    val = 0.95 if outcome.succeeded else 0.2
    return GroundTruthSignal(
        succeeded=outcome.succeeded,
        score=Score(ground_truth=val, components={"correctness": val}),
    )


def reflect(history, signal, reward):
    """Extract grounded linguistic guidance from a failed trajectory."""
    if signal.succeeded or not history:
        return None
    return Lesson(
        text=LESSON_HINT,
        episode=0,
        provenance=step_signature(history[-1]),
        support=1.0,
    )


def build_evaluator() -> Evaluator:
    """Rubric evaluator fixed within an epoch; it only emits the reward for reflection."""

    def score(o):
        truth = (1.0 if o.succeeded else 0.0) if hasattr(o, "succeeded") else o["truth"]
        return Score(ground_truth=truth)

    return Evaluator(score=score, name="honest-rubric", rubric=("correctness",))


def build_held_out() -> HeldOut:
    """Measurement substrate for evaluator promotion: fixed gold labels and raw production-task names."""
    return HeldOut(
        (
            Probe("hold-fail", {"truth": 0.0}, gold_label=0.0),
            Probe("hold-pass", {"truth": 1.0}, gold_label=1.0),
        )
    )


def run() -> object:
    """Demo body. Returns the Reflexion result; print is the only side effect for tests."""
    return run_reflexion(
        episode=make_episode(),
        ground_truth=ground_truth,
        reflect=reflect,
        evaluator=build_evaluator(),
        convergence=[RubricThreshold(target=0.8, sustain=1), MaxEpisodes(5)],
        declared_keys=DECLARED_KEYS,
        production_tasks=["fix-off-by-one"],
        held_out=build_held_out(),
        epoch_len=2,
    )


def main() -> None:
    result = run()
    print("=== loop-agent Reflexion demo ===")
    for rec in result.state.episodes:
        outcome = "PASS" if rec.succeeded else "fail"
        print(
            f"episode {rec.episode} (epoch {rec.epoch}): "
            f"ground_truth_aggregate={rec.gt_aggregate:.2f} [{outcome}] "
            f"reward={rec.reward:.2f} admitted_lesson={rec.admitted}"
        )
    print(
        f"-> status={result.status} succeeded={result.succeeded} "
        f"best_score={result.best_score:.2f} episodes={result.episodes}"
    )
    print(
        "The lesson learned from episode 0's failure was wired into episode 1's "
        "context, lifting ground-truth from 0.20 to 0.95."
    )


if __name__ == "__main__":
    main()
