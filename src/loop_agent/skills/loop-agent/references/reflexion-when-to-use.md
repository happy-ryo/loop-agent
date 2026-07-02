> This file is a load-on-demand bundled copy of `docs/reflexion-when-to-use.md`. The canonical source is `docs/reflexion-when-to-use.md` in the repository.

# When to Use Reflexion: Tasks Where It Helps and Tasks Where It Does Not

loop-agent can layer a Reflexion-style loop across attempts (`run_reflexion`) **outside** the inner `run_loop` (`gather -> act -> verify -> repeat`). Each episode is one execution of the inner loop. At episode boundaries, it extracts **linguistic guidance (lessons)** from failed trajectories and wires those lessons into the next episode's context.

The question is when adding Reflexion is worth it. The answer fits in one line:

> **Reflexion helps only on tasks with systematic failure. For stochastic failure, it performs almost the same as blind retry.**

This is not speculation. It is the conclusion confirmed by an actual run of the **Self-translation PoC**.

---

## Decision Criterion: Is Your Failure Systematic or Stochastic?

| | **systematic failure (suited to Reflexion)** | **stochastic failure (blind retry is enough)** |
|---|---|---|
| Nature | Each attempt repeats **the same conceptual mistake** | Each retry is **an independent probabilistic event** (something is missed by chance) |
| Examples | Persistent misunderstanding of the task, consistent mishandling of a specific syntax, breaking import order every time | Dropping one item at the end of a long input, partial edits, irregular model omissions |
| How lessons help | "Do this next time" **prevents the same kind of mistake on the next attempt** | The model already "knows" the rule and merely slipped -> lessons add little value |
| Correct response | **Reflexion** (wire lessons into the next episode) | **blind retry** (resampling usually passes) |

Judgment hint: check whether **failures are correlated across attempts**. If the same file or the same syntax fails **in the same way every time**, the failure is systematic. If the failure location varies and a retry produces a different result, it is stochastic.

---

## Evidence: Self-translation PoC (Run 1 vs Run 2)

We ran an actual comparison of **no-Reflexion and Reflexion** on a task that translated 10 files from loop-agent itself into English with `haiku`.

| | Run 1 (no Reflexion = blind retry) | Run 2 (Reflexion) |
|---|---|---|
| Result | 10/10 (`goal_met`) | 10/10 (`converged`) |
| Inner iterations | 13 | 14 (episode0: 10 + episode1: 4) |
| Wall clock | About 33 minutes | About 32 minutes |
| Tokens counted | 11.17M | 10.72M |
| Retry mechanism | blind round-robin retry | lesson-guided episode |

Run 2 episode breakdown:

| Episode | ground-truth aggregate | done | lesson admitted |
|---|---|---|---|
| 0 (one pass each, cap 10) | 0.60 | 6/10 | **yes** |
| 1 (after lesson wiring) | 1.00 | 10/10 | no |

**Finding: on this task, Reflexion did not significantly outperform blind retry.** Both converged to the same 10/10 at almost the same cost, within run-to-run noise.

The reason is the most useful result from this PoC:

- The initial failures were **stochastic**. On long files, `haiku` may drop one trailing comment or make a partial edit, but it does not make *the same conceptual mistake every time*. If blind retry resamples, the task usually passes. A lesson such as "you missed one comment" has little to grip, because the model already knows the rule and simply slipped.
- Reflexion's structural advantage, **not repeating systematic mistakes**, helps only when verify failures are **correlated across attempts**. This translation task was not like that. The outer loop's lesson channel worked correctly, but there was no suitable failure mode for it to act on.

**Plain reading**: the inner loop's mechanical retry plus ground-truth verify is enough for self-translation. Reflexion is a tool for tasks with **systematic failure modes**, not for stochastic slips.

> Note: both runs exercised the whole machine end to end: the inner gate + store + lease, and the outer episode + epoch boundary + episodic memory + grounded lesson admission. "Reflexion did not help" does not mean "the machine was broken"; it means "this task did not present a suitable target for Reflexion."

---

## Practical Workflow

1. **Run without Reflexion first** (`run_loop` plus limits such as `MaxIterations`). This is enough for many tasks.
2. **Inspect failure logs for correlation**. Look at `LoopObserver` JSONL or the steps in state.db and decide whether failures repeat in the same place and in the same way.
3. **Add Reflexion when failures are systematic** (`run_reflexion`). Lesson wiring saves iterations and cost only when failures are correlated across attempts.
4. **Model escalation is orthogonal**. If the difficulty comes from a weaker model lacking capability, the [ModelLadder pattern](https://github.com/happy-ryo/loop-agent/blob/main/docs/adapters/README.md), which escalates to a stronger model, is more effective than Reflexion. The two can be combined as a two-layer defense: lessons plus model escalation.

### Which Side Do These Task Types Usually Fall On?

- **Translation / docstring cleanup**: tends toward stochastic -> start with blind retry.
- **Flaky test stabilization**: if the causes share a common pattern, such as time dependence or order dependence, it is systematic -> suited to Reflexion. If each flaky failure is unrelated, it is stochastic.
- **Refactoring**: often repeats the same abstraction mistake or breaks the same imports -> tends toward systematic and is suited to Reflexion.
- **Bug fixing**: systematic if the agent keeps trying to fix the bug from the same incorrect hypothesis. This pairs well with the discipline of writing a reproduction test first.

---

## Safety Core (Prerequisites When Using Reflexion)

Even when using `run_reflexion`, loop-agent's safety core remains intact:

- **Two-signal model**: control outcomes such as convergence and admission are driven by the **ground-truth primary signal** (inner verify = test/lint/exit-code), while the rubric evaluator's `reward` is consumed only by `reflect`. The structure closes the loophole of increasing an evaluator scalar and declaring convergence.
- **Keep the evaluator fixed and prevent self-optimization**: freeze evaluation criteria within an epoch, and update them only at epoch boundaries when the candidate beats the incumbent by more than ε on agreement against fixed held-out gold labels.
- **Verify lessons before admission**: require grounding, and have the driver recompute and overwrite self-reported support, rejecting injected false lessons.

For details, see [docs/reflexion.md](https://github.com/happy-ryo/loop-agent/blob/main/docs/reflexion.md).
