# Outer Reflexion Loop + RQGM Epoch Safety Core

A self-improving mechanism that layers a Reflexion-style inter-attempt loop **outside** the inner ReAct loop (`run_loop`), incorporates linguistic guidance from failures into episodic memory, and wires that memory into the next episode's context. The safety core is the "two-signal model" and the "epoch promotion gate."

In the full design (report.md §4.4 / §5 Phase 3 / §6 / RQGM comments in Issues #22 and #4), a Reflexion-style inter-attempt loop is layered **outside** the inner ReAct loop. `run_reflexion(...)` calls the inner `run_loop` as **one episode** (the driver does not modify the inner loop), runs `reflect(trajectory, signal, reward)` at episode boundaries, incorporates the resulting **linguistic guidance (lesson)** into episodic memory, and wires it into the next episode's context. This demonstrates that learning from failed trajectories improves evaluation in the next loop (success condition a).

## Two-Signal Model (signal vs reward, Key Design Point, Safety Core)

Each episode produces two distinct signals.

- `signal` (**ground-truth first**): computed by the driver from inner verification (test/lint/exit-code) and `LoopResult.succeeded`.
  Every consequential control decision is driven by this signal: convergence, plateau detection, best selection, evaluator promotion, and lesson admission. This gives the control path a scale that does not depend on swapping evaluators.
- `reward` (the output of the rubric evaluator, **fixed within an epoch**): consumed **only by `reflect`** as verbal reinforcement for Reflexion. It is never used for convergence or admission decisions.

This structurally closes the escape hatch of "raising a gameable evaluator scalar and declaring convergence."

## Safety Invariants

Safety invariants (report.md §6 + RQGM; demonstrated in tests such as `tests/test_reflexion.py`, not just comments):

- **Do not let the system self-optimize against a fixed evaluator**: the epoch structure freezes the evaluation criteria within an epoch, and evaluator updates happen **only at epoch boundaries**. An update is allowed only when it beats the incumbent by more than ε on agreement with **fixed held-out gold labels** and does not regress on any fold or critical probe (ε-best-belief + dominance; `admit_evaluator`). `epoch_len>=2` and `epsilon>0` are enforced at construction time.
- **Ground truth comes first** (test/lint/exit-code); the judge is rubric-based and limited (`Score` aggregates with the minimum over diverse axes, missing axes are 0.0, and the judge is excluded from aggregation).
- **Early stopping** (stop on plateau via the best-so-far trend in `ScorePlateau`) / **diverse evaluation** / **dual-component separation** (the measurement path only scores pre-recorded probes and does not touch production act/gate paths; task-namespace features are validated at construction time) / **pre-admission memory validation** (`default_admit` requires grounding through a structural gate, and support is recomputed and overwritten by the driver, so self-reported support is not trusted; false lesson injection is rejected).
- **Prevent reflection bloat and degradation with iteration limits** (item/character/rendered-byte caps in `EpisodicMemory` + `ReflectionBudget` / `MaxEpisodes`).

```python
from loop_agent import (
    run_reflexion, Evaluator, Score, GroundTruthSignal, HeldOut, Probe,
    Lesson, MaxEpisodes, RubricThreshold, run_loop, ActOutcome, VerifyOutcome,
    MaxIterations,
)
from loop_agent.memory import step_signature

def episode(ctx):                                    # 1 episode = one inner run_loop call
    has_lesson = "increment by 1" in ctx.memory_block
    act = lambda _c: ActOutcome(observation="fixed" if has_lesson else "bug", tokens=5)
    verify = lambda o: VerifyOutcome(goal_met="fixed" in o.observation)
    return run_loop(act=act, verify=verify, conditions=[MaxIterations(2)])

def ground_truth(o):                                 # Primary signal comes from inner verify, not the evaluator
    v = 0.95 if o.succeeded else 0.2
    return GroundTruthSignal(succeeded=o.succeeded,
                             score=Score(ground_truth=v, components={"correctness": v}))

def reflect(history, signal, reward):                # Extract a grounded lesson from failure
    if signal.succeeded: return None
    return Lesson(text="increment by 1", episode=0,
                  provenance=step_signature(history[-1]), support=1.0)

result = run_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect,
    evaluator=Evaluator(score=lambda o: Score(ground_truth=1.0 if o.succeeded else 0.0),
                        name="rubric"),
    convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
    declared_keys=("correctness",),
    production_tasks=["fix-off-by-one"],
    held_out=HeldOut((Probe("h0", {"truth": 0.0}, 0.0), Probe("h1", {"truth": 1.0}, 1.0))),
    epoch_len=2,
)
# ep0 fails with empty memory (0.20) -> learns a lesson -> ep1 passes with wired-in guidance (0.95)
# result.succeeded is True / result.best_score == 0.95
```

## Outer Reflexion Persistence/Resume (epoch and lesson tables + evaluator version registry)

The outer loop's **learning state** (epoch progress, episodic-memory lessons, and the evaluator version fixed for each epoch) is persisted to state.db, allowing learning to **resume from where it left off after a restart** (Issue #29). Building on inner resume (`LoopStore.load_or_init` / #14) and store leases (#21), four outer-loop-specific tables (`reflexion_run` / `reflexion_episode` / `reflexion_lesson` / `reflexion_evaluator`) are added **independently from and additively to** the inner schema (`IF NOT EXISTS`).

- **Use settled state as the SoT**: the `persist` hook in `run_reflexion(..., persist=log.on_episode)` fires **only after each episode is fully settled** (after boundary processing, including epoch promotion and evaluator replacement). `DBReflexionLog` receives that state and writes the episode row, all lessons in memory, `reflexion_run` scalars, and evaluator version registration in **one transaction**. Resuming from an interruption matches an uninterrupted run (episode count / epoch / admitted lessons / evaluator version / best ground truth).
- **Evaluator version registry + fail-loud behavior**: the evaluator version fixed for each epoch is appended to `reflexion_evaluator` (audit), and `reflexion_run` stores the current version. On resume, if the restored `evaluator_version` differs from the supplied `evaluator.version`, `run_reflexion` **fails loudly** (callables cannot be serialized, so it does not silently swap in a different evaluator; this carries forward the safety core from PR #28). `declared_keys` must also match, preventing false convergence under stale aggregation.
- **Memory capacity policy round-trips too**: `cap` / `per_lesson_chars` / `render_byte_cap` are saved, and resume reconstructs an `EpisodicMemory` with the same limits, so eviction behavior is consistent across resume. A `paused` episode is not settled, so it is not persisted (resume can rerun the same episode).

```python
from loop_agent import DBReflexionLog, run_reflexion, MaxEpisodes

# First process: run 3 episodes, then interrupt (closing the connection is equivalent to process exit)
log = DBReflexionLog("outer.db", "run-1")          # Empty if new; restored partial state if existing
result = run_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect, evaluator=evaluator,
    convergence=[MaxEpisodes(3)], declared_keys=("correctness",),
    production_tasks=["fix"], held_out=held_out,
    initial_state=log.state, memory=log.memory, persist=log.on_episode,   # Persistence wiring
)
log.record_result(result); log.close()

# Second process: reopen the same DB and resume, preserving epoch, admitted lessons, and evaluator version
log2 = DBReflexionLog("outer.db", "run-1")          # Restore learning state from state.db
result2 = run_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect, evaluator=evaluator,
    convergence=[MaxEpisodes(6)], declared_keys=("correctness",),
    production_tasks=["fix"], held_out=held_out,
    initial_state=log2.state, memory=log2.memory, persist=log2.on_episode,
)
# result2 matches an uninterrupted MaxEpisodes(6) run in episode count, epoch, admitted lessons, evaluator version, and best
```

**Scope boundary**: focus on single-process self-improvement (distributed coordination is Issue #21). Outer-loop **persistence/resume** (epoch and lesson tables + evaluator version registry) has been **implemented in state.db (Issue #29; `ReflexionStore` / `DBReflexionLog`)**. Outer-loop **OTel observation** is also connected in [observability.md](./observability.md) (Issue #30; `run_observed_reflexion`). The remaining follow-up is dashboarding for observation (without touching the safety core: the two-signal model / epoch promotion gate / pre-admission validation).

## Related

- [README](../README.md) — Overall entry point and navigation summary
- [reflexion-when-to-use.md](./reflexion-when-to-use.md) — How to decide whether to use Reflexion or whether blind retry is sufficient
- [observability.md](./observability.md) — Outer Reflexion observation (`run_observed_reflexion`)
- [seams.md](./seams.md) — Details of seams such as act / verify
