"""Integration/invariant tests for the outer Reflexion driver + RQGM safety kernel (core of Issue #22).

This is the main body that demonstrates safety invariants with tests. Each invariant is locked down
with a **positive example** and a paired **falsification** where the attack succeeds if the guard is
removed (proving that the guard is load-bearing).
"""

from __future__ import annotations

import pytest

from loop_agent.conditions import StopTrigger
from loop_agent.convergence import MaxEpisodes, ReflectionBudget, RubricThreshold
from loop_agent.evaluator import Evaluator, HeldOut, Probe, Score, GroundTruthSignal
from loop_agent.loop import LoopResult
from loop_agent.memory import EpisodicMemory, Lesson, LessonVerdict, step_signature
from loop_agent.reflexion import ReflexionContext, run_reflexion
from loop_agent.state import LoopState, StepRecord


# -- Shared stubs ---------------------------------------------------------------

DECLARED = ("primary",)


def make_result(succeeded: bool, observation: object = "obs") -> LoopResult:
    """Result stand-in for the inner ``run_loop`` (switches goal_met/stopped by succeeded)."""
    step = StepRecord(iteration=0, observation=observation, tokens=1,
                      goal_met=succeeded, detail=str(observation))
    state = LoopState(iteration=1, history=[step], goal_met=succeeded)
    if succeeded:
        return LoopResult(status="goal_met", stop=None, state=state)
    return LoopResult(
        status="stopped",
        stop=StopTrigger(name="max_iterations", reason="cap"),
        state=state,
    )


def gt_from_success(hi: float = 0.9, lo: float = 0.2, backed: bool = True):
    """Build a primary signal from outcome.succeeded (from inner verify; not the evaluator)."""

    def gt(outcome):
        val = hi if outcome.succeeded else lo
        comps = {k: val for k in DECLARED}
        return GroundTruthSignal(
            succeeded=outcome.succeeded,
            score=Score(ground_truth=val, components=comps),
            ground_truth_backed=backed,
        )

    return gt


def _truth(o):
    """Read either EpisodeOutcome or probe dict so evaluators can score both."""
    if hasattr(o, "succeeded"):
        return 1.0 if o.succeeded else 0.0
    return o["truth"]


HONEST = Evaluator(score=lambda o: Score(ground_truth=_truth(o)), name="honest")
FLAT = Evaluator(score=lambda o: Score(ground_truth=0.5), name="flat")
LENIENT = Evaluator(score=lambda o: Score(ground_truth=1.0), name="lenient")


def held_out_matching(*golds: float) -> HeldOut:
    """Probes where gold==truth (honest matches exactly; flat/lenient diverge)."""
    return HeldOut(
        tuple(Probe(f"probe-{i}", {"truth": g}, gold_label=g) for i, g in enumerate(golds))
    )


def no_reflect(history, signal, reward):
    return None


def true_reflect(history, signal, reward):
    """Extract a grounded natural-language instruction from a failure trajectory (native Reflexion behavior)."""
    if signal.succeeded:
        return None
    return Lesson(text="use-the-fix", episode=0,
                  provenance=step_signature(history[0]), support=1.0)


def accept_all(lesson, outcome):
    return LessonVerdict(admit=True)


def reject_all(lesson, outcome):
    return LessonVerdict(admit=False, reason="control")


# ==============================================================================
# Construction-time validation
# ==============================================================================


def _base_kwargs(**override):
    kwargs = dict(
        episode=lambda ctx: make_result(False),
        ground_truth=gt_from_success(),
        reflect=no_reflect,
        evaluator=FLAT,
        convergence=[MaxEpisodes(2)],
        declared_keys=DECLARED,
        production_tasks=["task-a"],
        held_out=held_out_matching(0.2, 0.8),
    )
    kwargs.update(override)
    return kwargs


@pytest.mark.parametrize(
    "override",
    [
        {"epoch_len": 1},          # Degenerates into a moving evaluator.
        {"epsilon": 0.0},          # Loses anti-churn margin.
        {"epsilon": -0.1},
        {"declared_keys": ()},     # Loses diverse evaluation.
        {"production_tasks": []},
    ],
)
def test_constructor_validation_rejects_unsafe_config(override):
    with pytest.raises(ValueError):
        run_reflexion(**_base_kwargs(**override))


def test_dual_component_overlap_rejected():
    """Reject intersecting production task and held-out probe namespaces (dual-component separation)."""
    with pytest.raises(ValueError):
        run_reflexion(
            **_base_kwargs(
                production_tasks=["probe-0"],  # Collides with held_out_matching's case_id.
                held_out=held_out_matching(0.2, 0.8),
            )
        )


def test_epoch_len_one_is_moving_evaluator_rejected():
    """INV1 falsification: epoch_len==1 (per-episode updates = moving evaluator) is structurally forbidden."""
    with pytest.raises(ValueError):
        run_reflexion(**_base_kwargs(epoch_len=1))


# ==============================================================================
# INV1: the evaluator is frozen within each epoch (reward hacking prevention)
# ==============================================================================


def test_evaluator_frozen_within_epoch_updates_only_at_boundary():
    """evaluator_version/reward are invariant within an epoch and promote only at boundaries."""
    records = []
    run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),  # Same trajectory every time.
            ground_truth=gt_from_success(),
            evaluator=FLAT,                          # Constant reward=0.5.
            convergence=[MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.5, 1.0),
            epoch_len=3,
            propose_evaluator=lambda outer, inc: HONEST,  # Propose honest at every boundary.
            on_episode=lambda rec, st: records.append(rec),
        )
    )
    # epoch 0 (ep0-2): fixed to initial incumbent FLAT. reward is constant at 0.5.
    assert {r.evaluator_version for r in records[:3]} == {FLAT.version}
    assert [r.reward for r in records[:3]] == [0.5, 0.5, 0.5]
    # HONEST wins on held-out at the boundary and promotes -> epoch 1 (ep3-5) is fixed to HONEST.
    assert {r.evaluator_version for r in records[3:6]} == {HONEST.version}
    assert [r.reward for r in records[3:6]] == [1.0, 1.0, 1.0]
    # This demonstrates the freeze: reward does not move within an epoch and changes only at boundaries.


def test_no_evaluator_update_on_terminal_boundary():
    """Do not run promotion at a boundary that also satisfies convergence/termination."""

    def explode(outer, inc):
        raise AssertionError("propose_evaluator must not run on a terminal boundary")

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            ground_truth=gt_from_success(),
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],   # episode==2 fires at the epoch boundary (epoch_len=2).
            held_out=held_out_matching(0.0, 0.5, 1.0),
            epoch_len=2,
            propose_evaluator=explode,
        )
    )
    assert result.stop.name == "max_episodes"
    assert result.state.evaluator_version == HONEST.version  # Do not rewrite at termination.
    assert result.epochs == 0


def test_gaming_evaluator_rejected_at_boundary():
    """A lenient evaluator proposed at the boundary cannot promote because held-out agreement is low."""
    records = []
    run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            evaluator=HONEST,
            convergence=[MaxEpisodes(6)],
            held_out=held_out_matching(0.0, 0.3, 0.6),
            epoch_len=2,
            propose_evaluator=lambda outer, inc: LENIENT,  # Gaming candidate.
            on_episode=lambda rec, st: records.append(rec),
        )
    )
    # HONEST remains for all episodes (LENIENT is never adopted).
    assert {r.evaluator_version for r in records} == {HONEST.version}


# ==============================================================================
# INV3: the ground-truth primary signal drives control; evaluator scalar is reflect-only
# ==============================================================================


def test_convergence_reads_ground_truth_not_evaluator_reward():
    """Even if a lenient evaluator returns reward=1.0, it does not converge unless ground truth is met."""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),  # Ground-truth failure.
            ground_truth=gt_from_success(hi=0.9, lo=0.2),
            evaluator=LENIENT,                        # reward=1.0 (high but irrelevant).
            convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(4)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.succeeded is False
    assert result.stop.name == "max_episodes"
    assert result.best_score == pytest.approx(0.2)
    # reward is high but not on the control path (reflect-only).
    assert all(rec.reward == 1.0 for rec in result.state.episodes)


def test_unbacked_episodes_do_not_count_toward_convergence():
    """Episodes with ground_truth_backed=False do not count toward convergence."""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            ground_truth=gt_from_success(hi=0.99, backed=False),  # No real signal.
            evaluator=HONEST,
            convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(3)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.succeeded is False  # Even with a high aggregate, backed=False prevents convergence.
    assert result.state.gt_aggregate_history == []


def test_unbacked_episode_lesson_not_admitted():
    """Do not admit lessons from episodes without a real signal into memory (keep the next context clean)."""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False, observation="real-step"),
            ground_truth=gt_from_success(lo=0.2, backed=False),  # No real signal.
            reflect=true_reflect,  # Returns a grounded lesson.
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert len(result.state.memory) == 0  # backed=False means it is not admitted.


# ==============================================================================
# INV4b: pre-admission memory validation (reject false lesson injection / self-reported support)
# ==============================================================================


def poison_reflect(history, signal, reward):
    """Injected lesson with provenance not tied to a real step plus falsified support."""
    return Lesson(text="POISON", episode=0, provenance="step-99-fabricated", support=99.0)


def test_poison_lesson_rejected_by_default_admission():
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=poison_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert len(result.state.memory) == 0  # Injected lesson is not admitted to memory.
    assert "POISON" not in result.state.memory.render()


def test_poison_admission_is_load_bearing():
    """Falsification: replacing pre-admission validation with accept_all lets the injected lesson through."""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=poison_reflect,
            admit_lesson=accept_all,   # Remove the guard.
            evaluator=HONEST,
            convergence=[MaxEpisodes(1)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert len(result.state.memory) == 1  # Removing the guard admits poison, showing validation was effective.


def test_self_reported_support_is_overwritten():
    """Even if reflect falsifies support, the driver recomputes and overwrites it from grounding."""
    captured = {}

    def reflect_with_fake_support(history, signal, reward):
        # Real provenance, but support is falsely reported as 99.0.
        return Lesson(text="real lesson", episode=0,
                      provenance=step_signature(history[0]), support=99.0)

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=reflect_with_fake_support,
            evaluator=HONEST,
            convergence=[MaxEpisodes(1)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    (stored,) = result.state.memory.lessons()
    assert stored.support == 1.0  # Recomputed value, not the self-reported 99.0.


# ==============================================================================
# INV5: bound reflection growth with an iteration limit
# ==============================================================================


def test_reflection_budget_stops_outer_loop():
    """ReflectionBudget stops the outer loop when admitted lessons reach the cap."""
    counter = {"n": 0}

    def unique_reflect(history, signal, reward):
        counter["n"] += 1
        return Lesson(text=f"lesson {counter['n']}", episode=0,
                      provenance=step_signature(history[0]), support=1.0)

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=unique_reflect,
            evaluator=HONEST,
            convergence=[ReflectionBudget(2), MaxEpisodes(100)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.stop.name == "reflection_budget"
    assert result.state.reflections == 2


def test_admitted_lesson_stamped_with_actual_episode():
    """The driver overwrites reflect's placeholder episode=0 with the actual episode number."""

    def reflect_each(history, signal, reward):
        # The hook always returns placeholder episode=0 (it does not know the correct number).
        return Lesson(text=f"lesson at {history[0].detail}", episode=0,
                      provenance=step_signature(history[0]), support=1.0)

    counter = {"n": 0}

    def episode(ctx):
        counter["n"] += 1
        return make_result(False, observation=f"ep{counter['n']}")

    result = run_reflexion(
        **_base_kwargs(
            episode=episode,
            reflect=reflect_each,
            evaluator=HONEST,
            convergence=[MaxEpisodes(3)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    # Lessons from all 3 episodes are each stamped with the correct episode number.
    episodes = sorted(l.episode for l in result.state.memory.lessons())
    assert episodes == [0, 1, 2]


def test_memory_size_bounded_under_many_reflections():
    counter = {"n": 0}

    def unique_reflect(history, signal, reward):
        counter["n"] += 1
        return Lesson(text=f"lesson {counter['n']}", episode=0,
                      provenance=step_signature(history[0]), support=1.0)

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=unique_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(20)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
            memory=EpisodicMemory(cap=3),
        )
    )
    assert len(result.state.memory) == 3  # Bounded by cap.


# ==============================================================================
# INV: reflect exceptions are non-fatal
# ==============================================================================


def test_reflect_exception_is_non_fatal():
    def boom_reflect(history, signal, reward):
        raise RuntimeError("reflect blew up")

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=boom_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.episodes == 2  # The run does not fail because of the exception.
    assert all("reflect failed" in rec.detail for rec in result.state.episodes)


# ==============================================================================
# Success judgment is independent of trigger order
# ==============================================================================


@pytest.mark.parametrize(
    "conditions",
    [
        [MaxEpisodes(2), RubricThreshold(0.8, sustain=2)],
        [RubricThreshold(0.8, sustain=2), MaxEpisodes(2)],
    ],
)
def test_success_is_order_insensitive(conditions):
    """Even if success and the hard cap fire on the same guard, success is order-independent."""
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(True),
            ground_truth=gt_from_success(hi=0.9),
            evaluator=HONEST,
            convergence=conditions,
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.succeeded is True
    assert result.best_score == pytest.approx(0.9)


# ==============================================================================
# Main point: learning improves the next episode's ground truth (Phase3 success condition a)
# ==============================================================================


def succeed_if_lesson(ctx: ReflexionContext):
    """Memory-sensitive episode that succeeds when memory_block contains guidance from the prior attempt."""
    helped = "use-the-fix" in ctx.memory_block
    return make_result(helped, observation="fixed" if helped else "broken")


def test_real_lesson_improves_next_episode_ground_truth():
    """ep0 fails -> grounded lesson is admitted -> ep1 succeeds via memory wiring (improvement verified by eval)."""
    result = run_reflexion(
        **_base_kwargs(
            episode=succeed_if_lesson,
            ground_truth=gt_from_success(hi=0.9, lo=0.2),
            reflect=true_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    history = result.state.gt_aggregate_history
    assert history[0] == pytest.approx(0.2)  # ep0: memory is empty, so it fails.
    assert history[1] == pytest.approx(0.9)  # ep1: wired-in learning makes it succeed.


def test_memory_unwired_control_shows_no_improvement():
    """Falsification (attribution): reject_all blocks admission, so ep1 does not improve (= wiring is the cause)."""
    result = run_reflexion(
        **_base_kwargs(
            episode=succeed_if_lesson,
            ground_truth=gt_from_success(hi=0.9, lo=0.2),
            reflect=true_reflect,
            admit_lesson=reject_all,   # Do not admit learning into memory.
            evaluator=HONEST,
            convergence=[MaxEpisodes(2)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    history = result.state.gt_aggregate_history
    assert history[0] == pytest.approx(0.2)
    assert history[1] == pytest.approx(0.2)  # No improvement (memory is not wired in).


def test_paused_inner_episode_propagates_pause():
    """If the inner episode pauses at a human gate, the outer loop propagates the pause (Issue #15 contract)."""
    paused = LoopResult(
        status="paused", stop=None, state=LoopState(), pending={"gate_key": "gate-0"}
    )

    def boom_reflect(history, signal, reward):
        raise AssertionError("reflect must not run on a paused episode")

    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: paused,
            reflect=boom_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(5)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.status == "paused"
    assert result.paused is True
    assert result.pending == {"gate_key": "gate-0"}
    # Do not record or advance an incomplete episode (resume can rerun the same episode).
    assert result.state.episode == 0
    assert result.state.episodes == []
    assert "awaiting human decision" in result.reason


def test_real_humangate_pause_propagates_through_reflexion():
    """Integration: if the inner episode pauses at a real HumanGate(#21), the outer loop propagates the pause.

    #21 lease/executing semantics complete inside the PROCEED path, and an unresolved gate returns
    status="paused" + pending as before. The outer loop propagates that without score/reflect
    (demonstrating that dual-signal control and lease exactly-once coexist at episode boundaries).
    """
    from loop_agent import (
        ActOutcome,
        HumanGate,
        LoopStore,
        VerifyOutcome,
        connect,
        run_loop,
    )
    from loop_agent import MaxIterations as InnerMaxIterations

    store = LoopStore(connect(":memory:"))
    run_id = "episode-with-gate"

    def is_irreversible(action):
        return action == "deploy"

    def gather(_state):
        return "deploy"  # Always propose an irreversible action.

    def inner_act(_ctx):
        return ActOutcome(observation="deployed", tokens=1)

    def never(_o):
        return VerifyOutcome(goal_met=False)

    # No resolver -> a real HumanGate pauses on the unresolved gate.
    gate = HumanGate(on=is_irreversible, store=store, run_id=run_id)

    def episode(ctx):
        return run_loop(
            act=inner_act,
            verify=never,
            conditions=[InnerMaxIterations(3)],
            gather=gather,
            gate=gate,
        )

    def boom_reflect(history, signal, reward):
        raise AssertionError("reflect must not run on a paused episode")

    result = run_reflexion(
        **_base_kwargs(
            episode=episode,
            reflect=boom_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(3)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert result.status == "paused"
    assert result.paused is True
    assert result.pending is not None  # Propagates pending from the inner gate.
    assert result.state.episode == 0  # Does not advance an incomplete episode.


def test_resume_rejects_mismatched_evaluator_version():
    """Outer resume: loudly reject a mismatch between restored evaluator_version and the provided evaluator."""
    from loop_agent.reflexion import ReflexionState

    seed = ReflexionState(episode=4, epoch=2, evaluator_version="some-promoted-version")
    with pytest.raises(ValueError, match="resume"):
        run_reflexion(
            **_base_kwargs(
                episode=lambda ctx: make_result(False),
                evaluator=HONEST,  # version != "some-promoted-version"
                convergence=[MaxEpisodes(6)],
                held_out=held_out_matching(0.2, 0.8),
                epoch_len=2,
                initial_state=seed,
            )
        )


def test_resume_rejects_mismatched_declared_keys():
    """Loudly reject resume with different declared_keys because stale aggregates could falsely converge."""
    from loop_agent.reflexion import ReflexionState

    seed = ReflexionState(
        episode=2, epoch=1, evaluator_version=HONEST.version,
        declared_keys=("old_axis",), gt_aggregate_history=[0.9, 0.9],
    )
    with pytest.raises(ValueError, match="declared_keys"):
        run_reflexion(
            **_base_kwargs(
                episode=lambda ctx: make_result(False),
                evaluator=HONEST,
                declared_keys=("primary",),  # Different axis from seed.
                convergence=[MaxEpisodes(4)],
                held_out=held_out_matching(0.2, 0.8),
                epoch_len=2,
                initial_state=seed,
            )
        )


def test_resume_accepts_matching_evaluator_version():
    """Resume can continue when the evaluator matches the restored version."""
    from loop_agent.reflexion import ReflexionState

    seed = ReflexionState(episode=2, epoch=1, evaluator_version=HONEST.version)
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            evaluator=HONEST,
            convergence=[MaxEpisodes(4)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
            initial_state=seed,
        )
    )
    assert result.state.episode == 4  # Continues from restored episode=2 and stops at 4.


def test_resume_does_not_mutate_seed_state():
    """Do not use initial_state destructively (the caller's snapshot remains unchanged)."""
    from loop_agent.reflexion import ReflexionState

    seed = ReflexionState(episode=1, epoch=0, gt_aggregate_history=[0.2])
    seed_mem_len = len(seed.memory)
    result = run_reflexion(
        **_base_kwargs(
            episode=lambda ctx: make_result(False),
            reflect=true_reflect,
            evaluator=HONEST,
            convergence=[MaxEpisodes(3)],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
            initial_state=seed,
        )
    )
    # seed remains as it was initially after the run (only the internal copy advanced).
    assert seed.episode == 1
    assert seed.gt_aggregate_history == [0.2]
    assert len(seed.memory) == seed_mem_len
    # The copy has advanced.
    assert result.state.episode == 3
    assert result.state is not seed


def test_production_path_never_runs_held_out_probes():
    """dual-component: episode() receives only production tasks and never executes probes."""
    seen_tasks = []

    def spy_episode(ctx):
        seen_tasks.append(ctx.task)
        return make_result(False)

    run_reflexion(
        **_base_kwargs(
            episode=spy_episode,
            evaluator=HONEST,
            convergence=[MaxEpisodes(3)],
            production_tasks=["prod-x", "prod-y"],
            held_out=held_out_matching(0.2, 0.8),
            epoch_len=2,
        )
    )
    assert set(seen_tasks) <= {"prod-x", "prod-y"}
    assert all(not str(t).startswith("probe-") for t in seen_tasks)
