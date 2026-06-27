#!/usr/bin/env python3
"""観測層デモ: loop_begin/step/end を JSONL へ流し、終了理由/メトリクスを見る。

``run_observed_loop`` にループを通すと、loop_begin -> loop_step x N -> loop_end の
構造化イベントが sink へ流れる。ここでは journal 風の JSONL sink と in-memory sink を
同時に張り、終了理由と累積トークンが追えることを示す。OTel が入っていれば同じ run が
1 本の GenAI span にもなる（未導入環境では no-op に degrade）。

実行:

    python3 examples/observed_demo.py
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from claude_loop import (
    ActOutcome,
    JsonlEventSink,
    ListSink,
    MaxIterations,
    TokenBudget,
    VerifyOutcome,
    otel_available,
    read_events,
    run_observed_loop,
)


def main() -> None:
    counter = {"n": 0}

    def act(_ctx):
        counter["n"] += 1
        return ActOutcome(observation=f"did work #{counter['n']}", tokens=10)

    def verify(_outcome):
        done = counter["n"] >= 3
        return VerifyOutcome(goal_met=done, detail="converged" if done else "")

    mem = ListSink()
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "events.jsonl"
        result = run_observed_loop(
            act=act,
            verify=verify,
            conditions=[MaxIterations(5), TokenBudget(1000)],
            sinks=[JsonlEventSink(path), mem],
        )

        print(f"otel available: {otel_available()}")
        print(f"status: {result.status} / reason: {result.reason}")
        print(f"iterations: {result.iterations} / tokens_used: {result.tokens_used}")
        print("event timeline (from JSONL):")
        for rec in read_events(path):
            kind = rec["kind"]
            if kind == "loop_step":
                print(f"  {kind:10s} iter={rec['iteration']} tokens_used={rec['tokens_used']}")
            elif kind == "loop_end":
                print(f"  {kind:10s} status={rec['status']} stop={rec['stop']} reason={rec['reason']!r}")
            else:
                print(f"  {kind:10s} conditions={rec.get('conditions')}")


if __name__ == "__main__":
    main()
