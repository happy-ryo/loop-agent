"""Operational helpers built on loop-agent's existing emit/state surfaces.

This module keeps operations logic opt-in. It derives summaries and spike
signals from persisted state or completed steps, but never changes loop control
flow by itself.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from html import escape
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from .events import EventSink, LoopEvent, fan_out
from .loop import (
    ACT_TIMEOUT_OBSERVATION,
    REVIEW_TIMEOUT_OBSERVATION,
    VERIFY_TIMEOUT_OBSERVATION,
)
from .state import LoopState, StepRecord

LOOP_SPIKE = "loop_spike"


@dataclass(frozen=True)
class Spike:
    """A detected operational spike.

    ``kind`` is a stable discriminator such as ``"token"`` or
    ``"repeated_failure"``. ``detail`` is human-readable. ``payload`` carries the
    measured values for event sinks and dashboards.
    """

    kind: str
    detail: str
    payload: Mapping[str, Any]


def _median(values: Sequence[float]) -> Optional[float]:
    clean = [v for v in values if v > 0]
    if not clean:
        return None
    return float(statistics.median(clean))


def _get_failed(observation: Any) -> bool:
    if isinstance(observation, Mapping):
        return bool(observation.get("failed", False))
    return bool(getattr(observation, "failed", False))


def _key(value: Any) -> Any:
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


def detect_spikes(
    state: LoopState,
    *,
    elapsed_deltas: Sequence[float] = (),
    token_window: int = 5,
    latency_window: int = 5,
    multiplier: float = 3.0,
    repeated_failure: int = 3,
) -> list[Spike]:
    """Detect operational spikes from completed loop state.

    Detection is read-only and deterministic. It returns spikes for the latest
    completed step only, using the previous window as the baseline where
    possible. Callers decide whether to emit the result, stop the loop, or ignore
    it.
    """

    history = state.history
    if not history:
        return []

    latest = history[-1]
    spikes: list[Spike] = []

    previous_tokens = [float(r.tokens) for r in history[-(token_window + 1) : -1]]
    token_baseline = _median(previous_tokens)
    if (
        token_baseline is not None
        and latest.tokens > 0
        and latest.tokens > token_baseline * multiplier
    ):
        spikes.append(
            Spike(
                kind="token",
                detail=(
                    f"step tokens {latest.tokens} exceed {multiplier:g}x "
                    f"baseline {token_baseline:g}"
                ),
                payload={
                    "tokens": latest.tokens,
                    "baseline": token_baseline,
                    "multiplier": multiplier,
                },
            )
        )

    if elapsed_deltas:
        latest_latency = float(elapsed_deltas[-1])
        previous_latency = list(elapsed_deltas[-(latency_window + 1) : -1])
        latency_baseline = _median(previous_latency)
        if (
            latency_baseline is not None
            and latest_latency > 0
            and latest_latency > latency_baseline * multiplier
        ):
            spikes.append(
                Spike(
                    kind="latency",
                    detail=(
                        f"step latency {latest_latency:g}s exceeds {multiplier:g}x "
                        f"baseline {latency_baseline:g}s"
                    ),
                    payload={
                        "latency": latest_latency,
                        "baseline": latency_baseline,
                        "multiplier": multiplier,
                    },
                )
            )

    tail = history[-repeated_failure:] if repeated_failure > 0 else []
    if len(tail) == repeated_failure:
        if all(_get_failed(r.observation) for r in tail):
            spikes.append(
                Spike(
                    kind="repeated_failure",
                    detail=f"adapter failed {repeated_failure} times in a row",
                    payload={"repeat": repeated_failure},
                )
            )
        details = [_key(r.detail) for r in tail if r.detail]
        if len(details) == repeated_failure and len(set(details)) == 1:
            spikes.append(
                Spike(
                    kind="verify_detail",
                    detail=f"verify detail repeated {repeated_failure} times",
                    payload={"repeat": repeated_failure, "detail": tail[-1].detail},
                )
            )
        observations = [_key(r.observation) for r in tail]
        timeout_markers = {
            ACT_TIMEOUT_OBSERVATION,
            REVIEW_TIMEOUT_OBSERVATION,
            VERIFY_TIMEOUT_OBSERVATION,
        }
        if observations and all(o in timeout_markers for o in observations):
            spikes.append(
                Spike(
                    kind="timeout",
                    detail=f"timeout marker repeated {repeated_failure} times",
                    payload={"repeat": repeated_failure, "marker": tail[-1].observation},
                )
            )

    return spikes


class SpikeDetector:
    """Opt-in ``on_step`` observer that emits ``loop_spike`` events.

    It does not stop or slow the loop. To turn a spike into control flow, wrap
    the same predicate in a separate ``StopCondition`` or application policy.
    """

    def __init__(
        self,
        sinks: Sequence[EventSink],
        *,
        token_window: int = 5,
        latency_window: int = 5,
        multiplier: float = 3.0,
        repeated_failure: int = 3,
        on_error: Optional[Callable[[EventSink, LoopEvent, BaseException], None]] = None,
    ) -> None:
        self.sinks = tuple(sinks)
        self.token_window = token_window
        self.latency_window = latency_window
        self.multiplier = multiplier
        self.repeated_failure = repeated_failure
        self.on_error = on_error
        self._last_elapsed = 0.0
        self._elapsed_deltas: list[float] = []

    def on_step(self, _record: StepRecord, state: LoopState) -> None:
        delta = max(0.0, float(state.elapsed) - self._last_elapsed)
        self._last_elapsed = float(state.elapsed)
        self._elapsed_deltas.append(delta)
        for spike in detect_spikes(
            state,
            elapsed_deltas=self._elapsed_deltas,
            token_window=self.token_window,
            latency_window=self.latency_window,
            multiplier=self.multiplier,
            repeated_failure=self.repeated_failure,
        ):
            event = LoopEvent(
                kind=LOOP_SPIKE,
                iteration=state.iteration,
                elapsed=state.elapsed,
                payload={
                    "spike": spike.kind,
                    "detail": spike.detail,
                    **dict(spike.payload),
                },
            )
            fan_out(self.sinks, event, on_error=self.on_error)


@dataclass(frozen=True)
class AdapterFailureBreaker:
    """Stop after adapter observations report ``failed=True`` repeatedly."""

    repeat: int
    name: str = "adapter_failure_breaker"

    def check(self, state: LoopState) -> Optional[str]:
        tail = state.history[-self.repeat :] if self.repeat > 0 else []
        if len(tail) == self.repeat and all(_get_failed(r.observation) for r in tail):
            return f"adapter failed {self.repeat} times in a row"
        return None


@dataclass(frozen=True)
class VerifyDetailBreaker:
    """Stop after the same non-empty verify detail repeats."""

    repeat: int
    name: str = "verify_detail_breaker"

    def check(self, state: LoopState) -> Optional[str]:
        tail = state.history[-self.repeat :] if self.repeat > 0 else []
        details = [r.detail for r in tail if r.detail]
        if len(details) == self.repeat and len(set(details)) == 1:
            return f"verify detail repeated {self.repeat} times: {details[-1]}"
        return None


@dataclass(frozen=True)
class TimeoutMarkerBreaker:
    """Stop after graceful timeout markers repeat."""

    repeat: int
    name: str = "timeout_marker_breaker"

    def check(self, state: LoopState) -> Optional[str]:
        tail = state.history[-self.repeat :] if self.repeat > 0 else []
        markers = {
            ACT_TIMEOUT_OBSERVATION,
            REVIEW_TIMEOUT_OBSERVATION,
            VERIFY_TIMEOUT_OBSERVATION,
        }
        if len(tail) == self.repeat and all(r.observation in markers for r in tail):
            return f"timeout marker repeated {self.repeat} times"
        return None


@dataclass(frozen=True)
class PerStepTokenCap:
    """Stop when the latest completed step exceeds a per-step token limit."""

    limit: int
    name: str = "per_step_token_cap"

    def check(self, state: LoopState) -> Optional[str]:
        if not state.history:
            return None
        latest = state.history[-1]
        if latest.tokens > self.limit:
            return f"step {latest.iteration} used {latest.tokens} tokens > {self.limit}"
        return None


@dataclass(frozen=True)
class LaunchThrottleDecision:
    """Pure launch-throttle verdict."""

    allow: bool
    reason: str = ""


def launch_throttle_decision(
    *,
    running: int,
    max_running: Optional[int] = None,
    recent_spikes: int = 0,
    max_recent_spikes: Optional[int] = None,
) -> LaunchThrottleDecision:
    """Return whether a scheduler should launch another run.

    This is a pure helper: it does not inspect a DB or mutate state. The caller
    owns thresholds and enforcement.
    """

    if max_running is not None and running >= max_running:
        return LaunchThrottleDecision(
            False, f"running runs {running} >= max_running {max_running}"
        )
    if max_recent_spikes is not None and recent_spikes > max_recent_spikes:
        return LaunchThrottleDecision(
            False,
            f"recent spikes {recent_spikes} > max_recent_spikes {max_recent_spikes}",
        )
    return LaunchThrottleDecision(True, "allowed")


class ActHook(Protocol):
    def __call__(self, context: Any) -> Any:
        ...


def step_throttle(
    act: ActHook,
    *,
    delay_seconds: float,
    sleep: Callable[[float], None],
) -> ActHook:
    """Wrap an act hook with an explicit, injected sleep before each call."""

    if delay_seconds < 0:
        raise ValueError("delay_seconds must be >= 0")

    def wrapped(context: Any) -> Any:
        if delay_seconds:
            sleep(delay_seconds)
        return act(context)

    return wrapped


def state_from_steps(steps: Sequence[Mapping[str, Any]]) -> tuple[LoopState, list[float]]:
    """Build ``LoopState`` and elapsed deltas from persisted step rows."""

    history: list[StepRecord] = []
    elapsed_deltas: list[float] = []
    previous_elapsed = 0.0
    tokens_used = 0
    goal_met = False
    for step in steps:
        elapsed = float(step.get("elapsed", 0.0))
        elapsed_deltas.append(max(0.0, elapsed - previous_elapsed))
        previous_elapsed = elapsed
        tokens = int(step.get("tokens", 0))
        tokens_used = int(step.get("tokens_used", tokens_used + tokens))
        goal_met = bool(step.get("goal_met", False))
        history.append(
            StepRecord(
                iteration=int(step.get("iteration", len(history))),
                observation=step.get("observation"),
                tokens=tokens,
                goal_met=goal_met,
                detail=str(step.get("detail", "")),
            )
        )
    state = LoopState(
        iteration=len(history),
        tokens_used=tokens_used,
        elapsed=previous_elapsed,
        goal_met=goal_met,
        history=history,
    )
    return state, elapsed_deltas


def scan_spikes(
    steps: Sequence[Mapping[str, Any]],
    *,
    token_window: int = 5,
    latency_window: int = 5,
    multiplier: float = 3.0,
    repeated_failure: int = 3,
) -> list[tuple[int, Spike]]:
    """Post-hoc spike scan over persisted steps."""

    found: list[tuple[int, Spike]] = []
    for idx in range(1, len(steps) + 1):
        state, elapsed_deltas = state_from_steps(steps[:idx])
        for spike in detect_spikes(
            state,
            elapsed_deltas=elapsed_deltas,
            token_window=token_window,
            latency_window=latency_window,
            multiplier=multiplier,
            repeated_failure=repeated_failure,
        ):
            found.append((state.history[-1].iteration, spike))
    return found


def render_dashboard_html(
    *,
    runs: Sequence[Mapping[str, Any]],
    steps_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    pending_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    events_by_run: Mapping[str, Sequence[Mapping[str, Any]]],
    stop_by_run: Mapping[str, Optional[Mapping[str, Any]]],
    reflexion_runs: Sequence[Mapping[str, Any]] = (),
    reflexion_episodes_by_run: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
) -> str:
    """Render a standalone read-only operations dashboard."""

    reflexion_episodes_by_run = reflexion_episodes_by_run or {}

    def td(value: Any) -> str:
        return f"<td>{escape('-' if value is None else str(value))}</td>"

    run_rows = []
    for run in runs:
        run_id = str(run["run_id"])
        stop = stop_by_run.get(run_id)
        stop_text = ""
        if stop is not None:
            stop_text = f"{stop.get('name') or '-'}: {stop.get('reason') or ''}"
        run_rows.append(
            "<tr>"
            + td(run_id)
            + td(run.get("status"))
            + td(run.get("iterations"))
            + td(run.get("tokens_used"))
            + td(f"{float(run.get('elapsed', 0.0)):.3f}")
            + td(len(pending_by_run.get(run_id, ())))
            + td(len(events_by_run.get(run_id, ())))
            + td(stop_text)
            + "</tr>"
        )

    step_sections = []
    for run in runs:
        run_id = str(run["run_id"])
        rows = []
        for step in steps_by_run.get(run_id, ()):
            rows.append(
                "<tr>"
                + td(step.get("iteration"))
                + td(step.get("tokens"))
                + td(step.get("tokens_used"))
                + td(f"{float(step.get('elapsed', 0.0)):.3f}")
                + td(step.get("goal_met"))
                + td(step.get("detail"))
                + "</tr>"
            )
        step_sections.append(
            f"<h3>Steps: {escape(run_id)}</h3>"
            "<table><thead><tr><th>iteration</th><th>tokens</th>"
            "<th>tokens_used</th><th>elapsed</th><th>goal_met</th>"
            "<th>detail</th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table>"
        )

    pending_rows = []
    for run_id, rows in pending_by_run.items():
        for row in rows:
            pending_rows.append(
                "<tr>"
                + td(run_id)
                + td(row.get("gate_key"))
                + td(row.get("status"))
                + td(row.get("created_at"))
                + "</tr>"
            )

    reflexion_rows = []
    for run in reflexion_runs:
        run_id = str(run["run_id"])
        reflexion_rows.append(
            "<tr>"
            + td(run_id)
            + td(run.get("status"))
            + td(run.get("episode"))
            + td(run.get("epoch"))
            + td(run.get("evaluator_version"))
            + td(run.get("best_gt_aggregate"))
            + td(len(reflexion_episodes_by_run.get(run_id, ())))
            + td(run.get("reason"))
            + "</tr>"
        )

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>loop-agent operations dashboard</title>
<style>
body { font-family: system-ui, sans-serif; margin: 24px; color: #172026; }
table { border-collapse: collapse; width: 100%; margin: 12px 0 24px; }
th, td { border: 1px solid #ccd3da; padding: 6px 8px; text-align: left; }
th { background: #eef2f5; }
h1, h2, h3 { margin-top: 24px; }
code { background: #eef2f5; padding: 1px 4px; }
</style>
</head>
<body>
<h1>loop-agent operations dashboard</h1>
<p>Read-only snapshot generated from <code>state.db</code>.</p>
<h2>Runs</h2>
<table><thead><tr><th>run_id</th><th>status</th><th>iterations</th>
<th>tokens</th><th>elapsed</th><th>pending</th><th>events</th><th>stop</th>
</tr></thead><tbody>""" + "".join(run_rows) + """</tbody></table>
<h2>Step Timelines</h2>
""" + "".join(step_sections) + """
<h2>Pending Decisions</h2>
<table><thead><tr><th>run_id</th><th>gate_key</th><th>status</th><th>created_at</th>
</tr></thead><tbody>""" + "".join(pending_rows) + """</tbody></table>
<h2>Reflexion</h2>
<table><thead><tr><th>run_id</th><th>status</th><th>episode</th><th>epoch</th>
<th>evaluator</th><th>best</th><th>episodes</th><th>reason</th>
</tr></thead><tbody>""" + "".join(reflexion_rows) + """</tbody></table>
</body></html>
"""
