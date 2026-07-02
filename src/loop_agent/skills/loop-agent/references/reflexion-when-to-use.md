> This file is a load-on-demand bundled copy of `docs/reflexion-when-to-use.md`. The canonical source is `docs/reflexion-when-to-use.md` in the repository.

# When to Use Reflexion: Tasks Where It Helps and Where It Does Not

loop-agent can layer a Reflexion-style inter-attempt loop (`run_reflexion`) **outside** the inner `run_loop` (`gather -> act -> verify -> repeat`). Each episode is one execution of the inner loop. At episode boundaries, it extracts **linguistic guidance (`lesson`)** from failed trajectories and wires it into the next episode's context.

The question is: when is Reflexion worth adding? The answer fits in one line:

> **Reflexion helps only on tasks with systematic failure. For stochastic failure, it is almost indistinguishable from blind retry.**

This is not speculation. It is the conclusion confirmed by actual runs of the **Self-translation PoC**.

---

## Decision Criterion: Is Your Failure Systematic or Stochastic?

| | **systematic failure (good fit for Reflexion)** | **stochastic failure (blind retry is enough)** |
|---|---|---|
| Nature | Each attempt repeats **the same conceptual mistake** | Each retry is **an independent probabilistic event** (something happens to be missed) |
| Examples | Persistent misunderstanding of the task, consistent mishandling of a particular syntax, breaking import order every time | Dropping one trailing item in a long input, partial edits, sporadic omissions by the model |
| How lessons help | "Do this next time" **prevents the same kind of mistake on the next attempt** | The model already "knows" the rule and merely slipped -> the lesson is nearly useless |
| Correct response | **Reflexion** (wire the lesson into the next episode) | **blind retry** (resampling usually passes) |

Practical test: check whether **failures are correlated across attempts**. If the same file or the same syntax fails **in the same way every time**, the failure is systematic. If the failing location varies and a retry produces a different result, it is stochastic.

---

## Evidence: Self-translation PoC (Run 1 vs Run 2)

We ran an actual comparison between **no Reflexion and Reflexion** on a task that translated 10 files from loop-agent itself into English with `haiku`.

| | Run 1 (no Reflexion = blind retry) | Run 2 (Reflexion) |
|---|---|---|
| Result | 10/10 (`goal_met`) | 10/10 (`converged`) |
| Inner iterations | 13 | 14 (episode0: 10 + episode1: 4) |
| Wall clock | About 33 minutes | About 32 minutes |
| Token accounting | 11.17M | 10.72M |
| Retry mechanism | blind round-robin retry | lesson-guided episode |

Run 2 episode breakdown:

| Episode | ground-truth aggregation | done | lesson adopted |
|---|---|---|---|
| 0 (once each, cap 10) | 0.60 | 6/10 | **yes** |
| 1 (after lesson wiring) | 1.00 | 10/10 | no |

**Finding: on this task, Reflexion did not meaningfully outperform blind retry.** Both converged to the same 10/10 result at almost the same cost, within normal run-to-run noise.

The reason is the most useful result of this PoC:

- The initial failures were **stochastic**: `haiku` sometimes dropped one trailing comment in a long file or made a partial edit, but it did not make *the same conceptual mistake every time*. A blind retry usually passed after resampling. A lesson such as "you missed one comment" had little to latch onto because the model already knew the rule and merely slipped.
- Reflexion's structural advantage, *not repeating systematic mistakes*, only helps when verify failures are **correlated across attempts**. This translation task did not have that property, so the outer loop's lesson channel worked correctly but had no suitable target.

**The honest read**: the inner loop's mechanical retry plus ground-truth verification is enough for self-translation. Reflexion is a tool for **tasks with systematic failure modes**, not for stochastic slips.

> Note: both runs exercised the whole mechanism end to end: the inner gate + store + lease, and the outer episode + epoch boundary + episodic memory + grounded lesson admission. "Reflexion did not help" does not mean "the mechanism was broken"; it means "this task had no suitable target for Reflexion."

---

## Practical Workflow

1. **Run without Reflexion first** (`run_loop` plus limits such as `MaxIterations`). This is enough for many tasks.
2. **Inspect failure logs for correlation**. Use `LoopObserver` JSONL / state.db steps to decide whether failures are repeating in the same location and in the same way.
3. **Add Reflexion if the failure is systematic** (`run_reflexion`). Lesson wiring saves attempts and cost only when failures are correlated across attempts.
4. **Model escalation is orthogonal**. If the difficulty comes from a weaker model lacking capability, the [ModelLadder pattern](https://github.com/happy-ryo/loop-agent/blob/main/docs/adapters/README.md) (escalating to a stronger model) helps more than Reflexion. The two can be combined as a two-stage defense: lessons plus model escalation.

### Which Side Do These Task Types Usually Fall On?

- **Translation / docstring cleanup**: tends to be stochastic -> start with blind retry.
- **Flaky test stabilization**: if the failure cause is shared, such as time dependence or order dependence, it is systematic -> a good fit for Reflexion. If each flaky failure is unrelated, it is stochastic.
- **Refactoring**: often repeats the same abstraction mistake or import breakage -> tends to be systematic and a good fit for Reflexion.
- **Bug fixing**: systematic if the agent keeps trying to fix the bug from the same wrong hypothesis. This pairs well with the discipline of writing a reproduction test first.

---

## Safety Core (Prerequisites When Using Reflexion)

Even when using `run_reflexion`, loop-agent's safety core stays intact:

- **Two-signal model**: consequential control such as convergence and adoption is driven by the **ground-truth primary signal** (inner verify = test/lint/exit-code), while the rubric evaluator's `reward` is consumed only by `reflect`. This structurally closes the shortcut of "raise the evaluator scalar and declare convergence."
- **Keep the evaluator fixed; do not let it self-optimize**: within an epoch, freeze the evaluation criteria. Update only at epoch boundaries, and only when the candidate beats the incumbent by more than ε on agreement with fixed held-out gold labels.
- **Verify lessons before admission**: require grounding, and have the driver recompute and overwrite self-reported support, rejecting false lesson injection.

For details, see [docs/reflexion.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/reflexion.md).
