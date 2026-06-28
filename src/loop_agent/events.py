"""Structured loop events and their sink (report.md S4.5 "observability" / S5 Phase 2).

The observation layer emits 3 types of *structured events*: loop_begin / loop_step / loop_end.
Each event carries iteration number, cost/metrics, and exit reason, leaving enough information
for post-analysis of the entire loop lifetime (report.md S5 Phase 2 success condition (b)
"all exit reasons are retained in the journal and analyzable post-hoc").

Events flow to sinks. A sink is a minimal interface with only ``emit(event) -> None``,
just like claude-org's ``journal_append`` -- a "one event per line append":
:class:`JsonlEventSink` (journal-style event sink), :class:`ListSink` for tests/in-memory,
and :class:`CallableSink` that bridges to arbitrary functions.

This layer depends **only on the loop core** (does not tightly couple to state.db persistence details,
report.md S4.6). Time uses only the injected-clock ``elapsed`` from the loop,
and events become deterministic with respect to the given run (:mod:`loop_agent.progress`
uses the same approach).
"""

from __future__ import annotations

import json
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

# Event type (discriminator). Allows readers to filter without scattering string
# literals everywhere. Matches loop_begin / loop_step / loop_end task designations.
LOOP_BEGIN = "loop_begin"
LOOP_STEP = "loop_step"
LOOP_END = "loop_end"


def _jsonable(value: Any) -> Any:
    """Convert an arbitrary value to a JSON-representable form on a best-effort basis.

    Scalars and JSON-native containers pass through unchanged; everything else (custom observation
    objects, etc.) is stringified via ``repr``, ensuring one weird value doesn't break the entire
    event. Same approach as :func:`loop_agent.progress._to_jsonable` for predictable saved form
    (eagerly applies ``json.dumps(default=...)``).
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)


@dataclass(frozen=True)
class LoopEvent:
    """One structured loop event.

    ``kind`` is one of :data:`LOOP_BEGIN` / :data:`LOOP_STEP` / :data:`LOOP_END`.
    ``iteration`` is the iteration number (0 for begin, completed step number starting from 0 for step,
    total iteration count for end). ``elapsed`` is seconds since loop start (deterministic, from injected clock).
    ``payload`` is kind-specific fields (metrics, exit reason, etc.).
    """

    kind: str
    iteration: int
    elapsed: float
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Flatten to a JSON-friendly dict (for sink serialization)."""
        return {
            "kind": self.kind,
            "iteration": self.iteration,
            "elapsed": self.elapsed,
            **self.payload,
        }


@runtime_checkable
class EventSink(Protocol):
    """Minimal interface for receiving structured events.

    Implementation receives one :class:`LoopEvent` and records it in its own way. The observation layer
    calls emit on a best-effort basis (doesn't kill the loop on sink exceptions;
    see :class:`~loop_agent.observe.LoopObserver`), so implementations should ideally not raise exceptions.
    """

    def emit(self, event: LoopEvent) -> None:
        ...


# Hook to override how emit failures in sinks are handled (default: warn).
# The observation layer (:mod:`loop_agent.observe`) also shares this type.
SinkErrorHandler = Callable[[EventSink, LoopEvent, BaseException], None]


@dataclass
class ListSink:
    """A sink that buffers received events in an in-memory list (for tests / in-memory use)."""

    events: list[LoopEvent] = field(default_factory=list)

    def emit(self, event: LoopEvent) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[LoopEvent]:
        """Return only events of the specified kind, in write order (small helper)."""
        return [e for e in self.events if e.kind == kind]


class CallableSink:
    """A sink that bridges events to arbitrary ``callable(dict) -> None``.

    Minimal adapter for flowing to loggers or existing ``journal_append``-style functions.
    Events are passed as dict via :meth:`LoopEvent.to_dict`.
    """

    def __init__(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._fn = fn

    def emit(self, event: LoopEvent) -> None:
        self._fn(event.to_dict())


class JsonlEventSink:
    """An append-only JSON Lines sink (file version of claude-org's ``journal_append``).

    Appends one event per line and flushes per line, so external observers (or post-crash readers)
    see progress in real-time. Each line is independently parseable as a complete record, so
    appending becomes the durability unit. A crash loses only the final partial line; all prior
    events remain readable (:func:`read_events` tolerates that trailing incomplete line).

    Does not depend on state.db. Observation remains independent as an emit layer via a minimal,
    self-contained journal-style sink (report.md S4.6: observation independent as emit layer, #11 runs in parallel).
    """

    def __init__(self, path: "str | os.PathLike[str]") -> None:
        self.path = Path(path)
        # Create parent directory first so the first append doesn't fail with "folder doesn't exist"
        # (the file itself is lazily created on first write).
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: LoopEvent) -> None:
        line = json.dumps(event.to_dict(), ensure_ascii=False, default=repr)
        # Perform open-append-flush per record to simplify lifecycle
        # (no handle left open) and make lines the durability unit.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()


def read_events(path: "str | os.PathLike[str]") -> list[dict[str, Any]]:
    """Read back a JSONL event file in write order.

    Skips blank lines. The only tolerated corruption is "final record mid-write" -- the final line
    if it lacks a newline-terminator due to a crash. A corrupted line with newline-terminator, or
    corruption elsewhere, raises as a real consistency error to avoid silent data loss masking bugs.
    Returns an empty list if the file doesn't exist (same approach as :func:`loop_agent.progress.read_progress`,
    though the target is a different format: event sequence).
    """
    p = Path(path)
    if not p.exists():
        return []

    text = p.read_text(encoding="utf-8")
    # Writer always ends with '\n'. A missing terminator on the final line is the
    # "in-flight (crash truncated)" signature. A corrupted final line WITH terminator raises as genuine corruption.
    final_is_truncated = bool(text) and not text.endswith("\n")

    records: list[dict[str, Any]] = []
    # Split only by '\n' (writer's framing itself). ``str.splitlines`` also splits on
    # U+2028 / U+2029 / U+0085, and ensure_ascii=False in json.dumps emits those characters
    # in string values as-is, causing one record to split into two and mistakenly treated as corruption.
    # The '' left by a trailing newline is filtered out by strip.
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for idx, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if idx == len(lines) - 1 and final_is_truncated:
                break  # Tolerate incomplete final line (truncated by crash) and skip it
            raise
    return records


def fan_out(
    sinks: "tuple[EventSink, ...]",
    event: LoopEvent,
    *,
    on_error: Optional[SinkErrorHandler] = None,
) -> None:
    """Distribute one event to multiple sinks on a best-effort basis.

    Catches exceptions per-sink so observation failures don't kill the loop (observation is best-effort).
    By default, failures are surfaced via ``warnings.warn`` and not silently swallowed.
    Pass ``on_error(sink, event, exc)`` to override behavior (e.g., strict in tests).
    """
    for sink in sinks:
        try:
            sink.emit(event)
        except Exception as exc:  # noqa: BLE001 - observation is best-effort
            if on_error is not None:
                on_error(sink, event, exc)
            else:
                warnings.warn(
                    f"event sink {type(sink).__name__} failed to emit "
                    f"{event.kind!r}: {type(exc).__name__}: {exc}",
                    RuntimeWarning,
                    stacklevel=2,
                )
