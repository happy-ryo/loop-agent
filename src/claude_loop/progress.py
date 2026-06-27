"""Minimal external state: an append-only progress file (report.md S5 Phase 1).

The PoC keeps live loop state in memory (:mod:`claude_loop.state`); this module
externalises a *record* of each completed iteration to a JSON Lines file so the
loop's forward progress survives the process. It is the smallest possible
stand-in for the ``state.db`` SoT that Phase 2 introduces (report.md S4.6): one
self-contained JSON object per line, appended and flushed as each iteration
completes, plus a final line describing why the loop ended.

JSON Lines is chosen deliberately. Each line is a complete, independently
parseable record, so an append is the unit of durability: a crash mid-run
truncates at most the final partial line and every prior iteration stays
readable. :func:`read_progress` tolerates that trailing partial line.

Wiring it into a run is one line -- pass :meth:`ProgressLog.on_step` as the
driver's ``on_step`` observer, then record the terminal verdict::

    progress = ProgressLog(path)
    result = run_loop(act=..., verify=..., conditions=..., on_step=progress.on_step)
    progress.record_result(result)

The records use only the fields already on :class:`~claude_loop.state.StepRecord`
and :class:`~claude_loop.state.LoopState`, so no time source beyond the loop's
injected clock is consulted -- the file is fully deterministic for a given run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .state import LoopState, StepRecord

if TYPE_CHECKING:  # avoid an import cycle at runtime; only needed for typing
    from .loop import LoopResult

# Record discriminator values, kept as constants so readers can filter without
# hard-coding string literals at every call site.
STEP = "step"
RESULT = "result"


def _to_jsonable(value: Any) -> Any:
    """Best-effort coercion of an arbitrary value to something JSON can encode.

    Scalars and JSON-native containers pass through; anything else (a custom
    observation object, say) is rendered with ``repr`` so a single odd value can
    never abort the whole progress record. Mirrors ``json.dumps(default=...)``
    but applied eagerly so the stored shape is predictable.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    return repr(value)


class ProgressLog:
    """Append-only JSONL recorder of a single loop run.

    One ``"step"`` line is written per completed iteration and one ``"result"``
    line when the run ends. Lines are flushed as they are written so an external
    observer (or a post-crash reader) sees progress as it happens.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        # Create the parent directory eagerly so the first append cannot fail on
        # a missing folder; the file itself is created lazily on first write.
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _append(self, record: dict[str, Any]) -> None:
        line = json.dumps(record, ensure_ascii=False, default=repr)
        # Open-append-flush per record keeps the lifecycle trivial (no handle to
        # close) and is the crash-robust unit: the line is the durability unit.
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
            fh.flush()

    def on_step(self, record: StepRecord, state: LoopState) -> None:
        """Record one completed iteration. Matches the driver's ``StepHook``."""
        self._append(
            {
                "kind": STEP,
                "iteration": record.iteration,
                "tokens": record.tokens,
                "tokens_used": state.tokens_used,
                "elapsed": state.elapsed,
                "goal_met": record.goal_met,
                "detail": record.detail,
                "observation": _to_jsonable(record.observation),
            }
        )

    def record_result(self, result: "LoopResult") -> None:
        """Record the terminal verdict once the loop has returned."""
        self._append(
            {
                "kind": RESULT,
                "status": result.status,
                "stop": result.stop.name if result.stop is not None else None,
                "reason": result.reason,
                "iterations": result.iterations,
                "tokens_used": result.tokens_used,
                "elapsed": result.elapsed,
            }
        )


def read_progress(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Read back every record from a progress file, in write order.

    Blank lines are skipped. A trailing partial line (the signature of a crash
    mid-append) is tolerated and dropped; a corrupt line anywhere *before* the
    end is a real inconsistency and is raised, so silent data loss never hides a
    bug. Returns an empty list when the file does not exist.
    """
    p = Path(path)
    if not p.exists():
        return []

    records: list[dict[str, Any]] = []
    # Split on '\n' only -- the exact framing the writer uses. ``str.splitlines``
    # would additionally break on U+2028 / U+2029 / U+0085, which json.dumps
    # emits literally inside string values (ensure_ascii=False), so a single
    # valid record whose detail/observation carried one of those characters
    # would be torn into two halves and wrongly read as corruption. The trailing
    # '' left by a final newline is dropped by the ``ln.strip()`` filter.
    lines = [ln for ln in p.read_text(encoding="utf-8").split("\n") if ln.strip()]
    for idx, line in enumerate(lines):
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            if idx == len(lines) - 1:
                break  # tolerate a single truncated final record
            raise
    return records
