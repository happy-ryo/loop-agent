"""Structured loop events and their sinks (report.md S4.5 "Observability" / S5 Phase 2).

The observation layer emits the three event types loop_begin / loop_step / loop_end
as *structured events*. Each event carries the iteration number, cost/metrics, and
termination reason, leaving enough information for post-run analysis of the loop's
whole lifetime (report.md S5 Phase 2 success condition (b), "all termination
reasons remain in the journal and can be analyzed after the fact").

Events flow into sinks. A sink is the smallest interface with only
``emit(event) -> None``. This module provides :class:`JsonlEventSink`, which follows
claude-org ``journal_append`` by appending "one event per line" directly as a
journal-style event sink, :class:`ListSink` for tests/in-memory use, and
:class:`CallableSink` for bridging to arbitrary functions.

This layer **depends only on the loop core** and is not tightly coupled to state.db
persistence details (report.md S4.6). Time uses only ``elapsed`` from the loop's
injected clock, making events deterministic for a given run, following the same
policy as :mod:`loop_agent.progress`.
"""

from __future__ import annotations

import json
import math
import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

# Event kinds (discriminators). Keep them as constants so readers can filter without
# scattering string literals. These match the task-specified loop_begin / loop_step / loop_end.
LOOP_BEGIN = "loop_begin"
LOOP_STEP = "loop_step"
LOOP_END = "loop_end"


def _jsonable(value: Any) -> Any:
    """Best-effort conversion of any value into a JSON-representable shape.

    Scalars and JSON-native containers pass through unchanged. Everything else,
    such as custom observation objects, is stringified with ``repr`` so one unusual
    value does not break the whole event. This follows the same policy as
    :func:`loop_agent.progress._to_jsonable`, making the persisted shape predictable
    by eagerly applying what ``json.dumps(default=...)`` would do.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return repr(value)


@dataclass(frozen=True)
class LoopEvent:
    """A single structured loop event.

    ``kind`` is one of :data:`LOOP_BEGIN` / :data:`LOOP_STEP` / :data:`LOOP_END`.
    ``iteration`` is the iteration number: 0 for begin, the zero-based completed
    step number for step, and the total iteration count for end. ``elapsed`` is
    seconds since loop start, derived from the injected clock and therefore
    deterministic. ``payload`` holds kind-specific fields such as metrics and
    termination reasons.
    """

    kind: str
    iteration: int
    elapsed: float
    payload: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Collapse into a flat JSON-friendly dict for sink serialization."""
        return {
            "kind": self.kind,
            "iteration": self.iteration,
            "elapsed": self.elapsed,
            **self.payload,
        }


@runtime_checkable
class EventSink(Protocol):
    """Minimal interface for receiving structured events.

    Implementations receive one :class:`LoopEvent` and record it in their own way.
    The observation layer calls emit on a best-effort basis, so sink exceptions do
    not kill the loop; see :class:`~loop_agent.observe.LoopObserver`. Implementations
    should ideally avoid raising exceptions.
    """

    def emit(self, event: LoopEvent) -> None:
        ...


# Hook for replacing how sink emit failures are handled; the default is warn.
# The observation layer (:mod:`loop_agent.observe`) also shares this type.
SinkErrorHandler = Callable[[EventSink, LoopEvent, BaseException], None]


@dataclass
class ListSink:
    """Sink that stores received events in an in-memory list for tests/in-memory use."""

    events: list[LoopEvent] = field(default_factory=list)

    def emit(self, event: LoopEvent) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[LoopEvent]:
        """Small helper returning only events of the specified kind in write order."""
        return [e for e in self.events if e.kind == kind]


class CallableSink:
    """Sink that bridges events to any ``callable(dict) -> None``.

    This is the smallest adapter for sending events directly to loggers or existing
    ``journal_append``-style functions. Events are passed as dicts after
    :meth:`LoopEvent.to_dict`.
    """

    def __init__(self, fn: Callable[[dict[str, Any]], None]) -> None:
        self._fn = fn

    def emit(self, event: LoopEvent) -> None:
        self._fn(event.to_dict())


class JsonlEventSink:
    """Append-only JSON Lines sink, the file-backed form of claude-org ``journal_append``.

    It appends one event per line and flushes each line, so external observers and
    post-crash readers can see progress directly. Each line is a complete record
    that can be parsed independently, making each append the durability unit. A
    crash can lose only one partial trailing line; all earlier events remain
    readable because :func:`read_events` tolerates that tail.

    It does not depend on state.db. This is a minimal, self-contained journal-style
    sink that keeps observation independent as an emit layer (report.md S4.6:
    observation is independent as an emit layer and can run in parallel with #11).
    """

    def __init__(self, path: "str | os.PathLike[str]") -> None:
        self.path = Path(path)
        # Create the parent directory first so the initial append does not fail
        # because the folder is missing. The file itself is created lazily on first write.
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, event: LoopEvent) -> None:
        line = json.dumps(
            _jsonable(event.to_dict()),
            ensure_ascii=False,
            allow_nan=False,
            default=repr,
        )
        # Open, append, and flush per record to keep the lifecycle simple
        # with no persistent handle, and to make each line the durability unit.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()


def read_events(path: "str | os.PathLike[str]") -> list[dict[str, Any]]:
    """Read a JSONL event file back in write order.

    Blank lines are skipped. The only tolerated corruption is a truncated trailing
    record: a final line that was partially appended during a crash and lacks a
    newline terminator. Corrupt lines that do end in a newline, or corrupt lines
    anywhere except the tail, are raised as real inconsistencies so silent data loss
    does not hide bugs. If the file does not exist, this returns an empty list,
    following the same policy as :func:`loop_agent.progress.read_progress` for a
    different format: an event sequence.
    """
    p = Path(path)
    if not p.exists():
        return []

    text = p.read_text(encoding="utf-8")
    # The writer always terminates records with '\n'. Only a trailing record without
    # that terminator is the signature of an in-progress write cut off by a crash.
    # A terminated but corrupt final line is raised as real corruption.
    final_is_truncated = bool(text) and not text.endswith("\n")

    records: list[dict[str, Any]] = []
    # Split only on '\n', which is the writer's exact framing. ``str.splitlines``
    # also splits on U+2028 / U+2029 / U+0085; because json.dumps with
    # ensure_ascii=False can emit those characters directly inside string values,
    # one record could be split into two and wrongly treated as corruption. The ''
    # left by a trailing newline is removed by the strip filter.
    lines = [ln for ln in text.split("\n") if ln.strip()]
    for idx, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if idx == len(lines) - 1 and final_is_truncated:
                break  # Tolerate and drop only one unterminated trailing line.
            raise
    return records


def fan_out(
    sinks: "tuple[EventSink, ...]",
    event: LoopEvent,
    *,
    on_error: Optional[SinkErrorHandler] = None,
) -> None:
    """Fan out one event to multiple sinks on a best-effort basis.

    Exceptions are caught per sink, so observation concerns do not kill the loop.
    By default, failures are made visible with ``warnings.warn`` and are not
    silently swallowed. Passing ``on_error(sink, event, exc)`` replaces that behavior,
    for example to make tests strict.
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
