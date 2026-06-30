"""Operational helpers built on loop-agent's existing emit/state surfaces.

This module keeps operations logic opt-in. It derives summaries and spike
signals from persisted state or completed steps, but never changes loop control
flow by itself.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence

from .events import EventSink, LoopEvent, fan_out
from .loop import ACT_TIMEOUT_OBSERVATION, VERIFY_TIMEOUT_OBSERVATION
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
        timeout_markers = {ACT_TIMEOUT_OBSERVATION, VERIFY_TIMEOUT_OBSERVATION}
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
