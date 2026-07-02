# Observability (loop events / OTel spans / outer Reflexion observations)

This observability layer emits the LoopAgent loop lifecycle as structured events and OpenTelemetry spans. It observes both the inner loop (`run_observed_loop`) and the outer Reflexion loop (`run_observed_reflexion`) in the same style.

## loop_begin / loop_step / loop_end + OTel span

This observability layer emits the loop lifecycle as **structured events**. When a loop runs through `run_observed_loop`, `loop_begin` -> `loop_step` x N -> `loop_end` events are sent to the **sink**. Each event carries the iteration number, cumulative tokens, elapsed time, and **termination reason**, so later analysis can determine why and how the loop ended. If OTel is available, the same run also becomes a single **GenAI span** (`gen_ai.*` + iteration number + termination reason).

```python
from loop_agent import run_observed_loop, JsonlEventSink, ListSink, read_events, MaxIterations

mem = ListSink()                                  # in-memory (for tests/inspection)
result = run_observed_loop(
    act=act, verify=verify,
    conditions=[MaxIterations(5)],
    sinks=[JsonlEventSink("events.jsonl"), mem],  # journal-style JSONL + in-memory (multiple sinks allowed)
)

events = read_events("events.jsonl")              # loop_begin / loop_step x N / loop_end
end = mem.of_kind("loop_end")[0]
print(end.payload["status"], end.payload["stop"], end.payload["reason"])
# "stopped" "max_iterations" "reached max iterations (5/5)"
```

- **Every termination reason is recorded in `loop_end`**: `goal_met` / `max_iterations` / `token_budget` / `timeout`, and even `error` when the loop body exits with an exception, are recorded as `status` / `stop` / `reason`.
- **Metrics remain traceable**: `loop_step` carries the iteration number, `tokens`, cumulative `tokens_used`, and `elapsed`, and stays consistent with the aggregate values in `loop_end`.
- **OTel is an optional dependency**: Even when it is not installed, `LoopSpan` **degrades to a no-op**, while JSONL / event sinks continue to work as-is. To install the SDK and inspect real spans, use `pip install -e .[dev]` (or `.[otel]`).
- **Same style as the existing `ProgressLog`**: For manual wiring, use `LoopObserver` as a context manager, pass `on_step` to `run_loop`, and call `record_result(result)` (`sink` exceptions are downgraded to warnings on a best-effort basis and do not kill the loop).

For a runnable demo, see [`examples/observed_demo.py`](../examples/observed_demo.py).

## Outer Reflexion Observability (episode/epoch/lesson/evaluator/convergence + OTel span)

The Phase 3 follow-up (extending the observability described in report.md §4.5 to the **outer loop** / Issue #30) observes the inter-trial lifecycle of outer `run_reflexion` in the **same style** as the inner-loop observability layer (`run_observed_loop` / `LoopObserver` / `LoopSpan`). When a loop runs through `run_observed_reflexion(...)`, inter-trial transitions are recorded as **structured events** (sent to the same sinks as `loop_*`). If OTel is available, the same run also becomes a single **GenAI span** (`gen_ai.*` + epoch number + evaluator version = grader id + lesson-derived provenance). **Observability is a side channel and does not change any decision logic in the two-signal model / RQGM epoch gate**; the existing safety core remains intact, with only observation hooks added.

Structured events emitted:

- `reflexion_begin` ... run start (convergence condition names, declared axes, initial evaluator version, epoch configuration)
- `episode_begin` / `episode_end` ... start / finalization of one episode (primary aggregate, reward, success/failure, lesson acceptance)
- `lesson_decision` ... only for episodes that produced a lesson. Records **accepted / rejected** independently to make filtering easier.
- `epoch_boundary` ... epoch boundary (= start of a new epoch) + decision to **promote / reject / keep unchanged** the evaluator
- `reflexion_end` ... run end (**convergence reason**, status, aggregates; derived from `result.state` for consistency)

```python
from loop_agent import (
    run_observed_reflexion, JsonlEventSink, ListSink, read_events,
    Evaluator, Score, GroundTruthSignal, HeldOut, Probe, Lesson,
    MaxEpisodes, RubricThreshold, run_loop, ActOutcome, VerifyOutcome, MaxIterations,
)
from loop_agent.memory import step_signature

mem = ListSink()
result = run_observed_reflexion(
    episode=episode, ground_truth=ground_truth, reflect=reflect,   # same hooks as the previous section
    evaluator=Evaluator(score=lambda o: Score(ground_truth=1.0 if o.succeeded else 0.0),
                        name="rubric"),
    convergence=[RubricThreshold(0.8, sustain=1), MaxEpisodes(5)],
    declared_keys=("correctness",), production_tasks=["fix-off-by-one"],
    held_out=HeldOut((Probe("h0", {"truth": 0.0}, 0.0), Probe("h1", {"truth": 1.0}, 1.0))),
    epoch_len=2,
    sinks=[JsonlEventSink("reflexion.jsonl"), mem],  # journal-style JSONL + in-memory (multiple sinks allowed)
)

events = read_events("reflexion.jsonl")              # reflexion_begin / episode_* / ... / reflexion_end
end = mem.of_kind("reflexion_end")[0]
print(end.payload["status"], end.payload["stop"], end.payload["reason"])
# "converged" "rubric_threshold" "rubric threshold reached: last 1 ground-truth aggregates all >= 0.8"
```

- **Every transition is recorded**: episode start/end, epoch start/boundary, lesson acceptance/rejection, **grader (evaluator) promotion/rejection**, and convergence reasons are recorded in events and span events, enabling post-run analysis of the outer loop lifecycle.
- **Metric consistency**: The emitted event counts (`episode_end` x N) and final aggregates (`reflexion_end` / span end attributes) are always consistent because they are derived from the authoritative `result.state`.
- **OTel is an optional dependency**: Even when it is not installed, `ReflexionSpan` **degrades to a no-op**, while JSONL / event sinks continue to work as-is (the same policy as the inner `LoopSpan`). To install the SDK and inspect real spans, use `pip install -e .[dev]` (or `.[otel]`).
- **Best-effort behavior**: sink / tracer / observation-hook exceptions do not kill the outer driver (sink exceptions are downgraded to warnings, span exceptions are swallowed and turned into no-ops, and hook body exceptions are also swallowed). Runs that exit with an exception leave a `reflexion_end` with `status="error"`; runs whose inner episode pauses at a human gate leave a `reflexion_end` with `status="paused"`.
- **Manual wiring is also possible**: Use `ReflexionObserver` as a context manager and wire it to `run_reflexion` via `on_episode` / `on_epoch` plus `on_episode_begin` immediately before `episode` (the same relationship that `LoopObserver` has to `run_observed_loop`).

**Scope boundary**: This page covers the **observability emission layer** (events + connection to OTel GenAI spans). **Dashboards** (visualization pipelines to Grafana and similar tools) and **3x spike auto-throttling** (automatic control using observed values, meaning feedback into the outer loop) are tracked in the [operations roadmap](./operations-roadmap.md).

## Related

- [README](../README.md) — overall entry point and navigation summary
- [reflexion.md](./reflexion.md) — how the outer Reflexion loop works
- [seams.md](./seams.md) — details of seams such as act / verify
- [operations-roadmap.md](./operations-roadmap.md) — operational helpers for summary / dashboard / spike detection / throttling / circuit breaker
