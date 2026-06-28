#!/usr/bin/env python3
"""Self-translation PoC: loop-agent translates its own source with loop-agent.

Issue #37 dogfood. This harness drives loop-agent's *own* loop core
(``run_loop`` / ``run_gated_loop`` / ``run_reflexion``) and the
``ClaudeCodeAct`` adapter to translate the Japanese docstrings and comments of
ten ``src/loop_agent`` modules into English -- without changing any code, public
API, or test name.

Architecture (report.md S4.4 gather -> act -> verify):

* ``gather`` picks the next not-yet-done file and renders a translation prompt
  (optionally weaving in Reflexion lessons from prior episodes).
* ``act``    is :class:`~loop_agent.adapters.claude_code.ClaudeCodeAct`: it
  launches ``claude --print`` with Read/Edit tools to translate the file in
  place. The whole point of the dogfood is that the *adapter does the work*.
* ``verify`` is the three-stage ground-truth check in :mod:`verify`
  (parses_ok -> no Japanese in comments/docstrings -> module pytest passes).
  Only all-three-pass marks a file done; the loop ends naturally when every
  file is done.

A :class:`~loop_agent.gate.HumanGate` wraps every translation step and is
auto-approved by a resolver (the PoC's "human = secretary delegate" runs the
approval cycle itself). Stop conditions cap the run at ``MaxIterations(20)`` /
``TokenBudget(2_000_000)`` / ``GoalMet(done == files)``.

The Reflexion pass (``--reflexion``) wraps the inner loop in
:func:`~loop_agent.reflexion.run_reflexion`: each episode re-attempts the still
failing files, and ``reflect`` turns a failed verify trajectory into a language
lesson wired into the next episode's prompt.

Run logs are written as JSON Lines via
:class:`~loop_agent.events.JsonlEventSink`.

CLI:

    python3 examples/self_translation_poc/harness.py --selfcheck      # fast, no claude
    python3 examples/self_translation_poc/harness.py --real           # run 1 (no Reflexion)
    python3 examples/self_translation_poc/harness.py --real --reflexion  # run 2 (Reflexion)

Output strings are ASCII only (cp932-safe ``--help`` / ``print``).
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

# Make ``loop_agent`` importable when run straight from a checkout.
_REPO_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_REPO_ROOT / "src"), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from loop_agent import (  # noqa: E402
    ActOutcome,
    Decision,
    Evaluator,
    GoalMet,
    GroundTruthSignal,
    HeldOut,
    JsonlEventSink,
    Lesson,
    LoopObserver,
    LoopResult,
    LoopStore,
    MaxEpisodes,
    MaxIterations,
    Probe,
    RubricThreshold,
    Score,
    Timeout,
    TokenBudget,
    VerifyOutcome,
    connect,
    run_gated_loop,
)
from loop_agent.adapters.claude_code import ClaudeCodeAct  # noqa: E402
from loop_agent.memory import step_signature  # noqa: E402
from loop_agent.reflexion_observe import run_observed_reflexion  # noqa: E402
from loop_agent.state import LoopState, StepRecord  # noqa: E402

import verify as V  # noqa: E402

REPO_ROOT = _REPO_ROOT
SRC = REPO_ROOT / "src" / "loop_agent"

# The ten translation targets (all under src/loop_agent, all have Japanese in
# comments/docstrings, all have a dedicated test module). Aligned with Issue
# #37's recommended candidates where those actually carry Japanese.
TARGET_FILES: tuple[Path, ...] = (
    SRC / "waker.py",
    SRC / "convergence.py",
    SRC / "observe.py",
    SRC / "events.py",
    SRC / "memory.py",
    SRC / "evaluator.py",
    SRC / "adapters" / "claude_code.py",
    SRC / "reflexion_store.py",
    SRC / "transport.py",
    SRC / "gate.py",
)

PROMPT_TEMPLATE = """\
You are translating Japanese into English inside a single Python source file, in place.

File: {file}

STRICT rules:
- Translate ALL Japanese in COMMENTS (lines/segments after `#`) and in DOCSTRINGS
  (module / class / function `\"\"\"..\"\"\"`) into natural, precise technical English.
- Do NOT change any code: identifiers, logic, control flow, signatures, public API,
  imports, decorators, or test names.
- Do NOT modify NON-docstring string literals. User-facing message strings may
  contain Japanese -- leave those byte-for-byte unchanged (they are out of scope).
- Preserve indentation, line structure, and Sphinx/reST cross-references intact:
  `:class:`...``, `:func:`...``, `:meth:`...``, `report.md S4.4`, `Issue #21`, `R6`.
- Be exhaustive. Every inline trailing comment counts. No Japanese may remain in
  any comment or docstring when you are done.
{lessons}
Read {file} with the Read tool, then use Edit to apply the translation directly to
the file. Do not print the file back to me; just edit it.
"""


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------
# The translator: gather + verify, holding the cross-iteration "done" set.
# --------------------------------------------------------------------------


class Translator:
    """Stateful gather/verify pair over a fixed set of target files.

    ``gather`` selects the next not-done file and renders its prompt; ``verify``
    runs the three-stage check on the file selected this iteration and records a
    file as done when it passes. The ``done`` set persists across Reflexion
    episodes, so a later episode only re-attempts files still failing.
    """

    def __init__(
        self,
        files: Sequence[Path],
        *,
        run_tests: bool = True,
        lessons: str = "",
    ) -> None:
        self.files: list[Path] = [Path(f) for f in files]
        self.run_tests = run_tests
        self.lessons = lessons
        self.done: list[str] = []
        self._current: Optional[Path] = None
        self.reports: list[V.VerifyReport] = []
        self._attempts: dict[str, int] = {str(f): 0 for f in self.files}

    def remaining(self) -> list[Path]:
        done = set(self.done)
        return [f for f in self.files if str(f) not in done]

    def all_done(self) -> bool:
        return len(self.done) == len(self.files)

    def gather(self, _state: LoopState) -> dict[str, str]:
        rem = self.remaining()
        # Round-robin by fewest attempts so a stubborn file never starves the
        # others: every file gets a first attempt before any file gets a retry.
        if rem:
            self._current = min(
                rem, key=lambda f: (self._attempts[str(f)], self.files.index(f))
            )
            self._attempts[str(self._current)] += 1
        else:
            self._current = self.files[-1]
        lessons_block = ""
        if self.lessons.strip():
            lessons_block = (
                "\nLessons learned from previous attempts (apply them):\n"
                + self.lessons.strip()
                + "\n"
            )
        prompt = PROMPT_TEMPLATE.format(file=_rel(self._current), lessons=lessons_block)
        # Context is JSON-native (str/str) so the HumanGate action guard accepts it.
        return {"prompt": prompt, "file": str(self._current)}

    def verify(self, _outcome: ActOutcome) -> VerifyOutcome:
        assert self._current is not None
        report = V.verify_file(self._current, run_tests=self.run_tests)
        self.reports.append(report)
        if report.done and str(self._current) not in self.done:
            self.done.append(str(self._current))
        return VerifyOutcome(
            goal_met=self.all_done(),
            detail=f"{report.summary()} | done {len(self.done)}/{len(self.files)}",
        )


# --------------------------------------------------------------------------
# act builders.
# --------------------------------------------------------------------------


def build_real_act(*, model: str, timeout: float) -> ClaudeCodeAct:
    """ClaudeCodeAct that launches ``claude --print`` to translate a file in place."""
    return ClaudeCodeAct(
        allowed_tools=["Read", "Edit"],
        permission_mode="acceptEdits",
        model=model,
        output_format="json",
        prompt_template="{prompt}",
        timeout=timeout,
        cwd=str(REPO_ROOT),
    )


def make_stub_act(
    transform: Callable[[Path], None],
    *,
    tokens_per_call: int = 1000,
) -> Callable[[dict[str, Any]], ActOutcome]:
    """A no-subprocess ``act`` for deterministic tests: mutate the file locally.

    ``transform(path)`` edits the file the way a translator would (used by the
    test suite to exercise the loop / gate / Reflexion mechanics without claude).
    """

    def _act(context: dict[str, Any]) -> ActOutcome:
        path = Path(context["file"])
        transform(path)
        return ActOutcome(observation={"file": context["file"]}, tokens=tokens_per_call)

    return _act


# --------------------------------------------------------------------------
# Run 1: single inner loop, HumanGate auto-approved, JSONL logged.
# --------------------------------------------------------------------------


def _auto_approve(_pending: dict[str, Any]) -> Decision:
    """The PoC's delegated human: approve every gated action."""
    return Decision("approve")


@dataclass
class RunResult:
    """Metrics captured from one PoC run (for the report comparison)."""

    label: str
    status: str
    succeeded: bool
    iterations: int
    tokens: int
    elapsed: float
    done: list[str]
    failed: list[str]
    log_path: str
    episodes: int = 0
    reason: str = ""
    failure_notes: list[str] = field(default_factory=list)
    episode_records: list[dict] = field(default_factory=list)


def run_no_reflexion(
    translator: Translator,
    act: Callable[[dict[str, Any]], ActOutcome],
    *,
    log_path: Path,
    max_iterations: int = 20,
    token_budget: int = 2_000_000,
    timeout: float = 6_000.0,
    gate: bool = True,
    store_path: Optional[Path] = None,
) -> RunResult:
    """Run the single-pass (no Reflexion) translation loop.

    The HumanGate gates every translation action and a resolver auto-approves it
    -- deliberately exercising the gate + LoopStore decision/lease lifecycle each
    iteration. (Translation edits are git-reversible, so a production config
    would instead reserve the gate for commit/push; see the report.)
    """
    conditions = [
        MaxIterations(max_iterations),
        TokenBudget(token_budget),
        Timeout(timeout),
        GoalMet(lambda _s: translator.all_done()),
    ]
    observer = LoopObserver(sinks=[JsonlEventSink(log_path)], conditions=conditions)
    start = time.monotonic()
    with observer:
        if gate:
            run_id = f"self-translation-{uuid.uuid4().hex[:12]}"
            db = store_path or (
                REPO_ROOT
                / "examples"
                / "self_translation_poc"
                / f".gate-{run_id}.sqlite3"
            )
            store = LoopStore(connect(db))
            result = run_gated_loop(
                act=act,
                verify=translator.verify,
                conditions=conditions,
                gather=translator.gather,
                on=lambda _ctx: True,  # every translation passes the gate...
                resolver=_auto_approve,  # ...and is auto-approved (delegated human)
                store=store,
                run_id=run_id,
                on_step=observer.on_step,
            )
        else:
            from loop_agent import run_loop

            result = run_loop(
                act=act,
                verify=translator.verify,
                conditions=conditions,
                gather=translator.gather,
                on_step=observer.on_step,
            )
        observer.record_result(result)
    elapsed = time.monotonic() - start

    done = list(translator.done)
    failed = [str(f) for f in translator.files if str(f) not in set(done)]
    return RunResult(
        label="no-reflexion",
        status=result.status,
        succeeded=result.succeeded,
        iterations=result.iterations,
        tokens=result.tokens_used,
        elapsed=elapsed,
        done=[_rel(Path(p)) for p in done],
        failed=[_rel(Path(p)) for p in failed],
        log_path=_rel(log_path),
        reason=result.reason,
        failure_notes=_collect_failure_notes(translator),
    )


def _collect_failure_notes(translator: Translator) -> list[str]:
    """Human-readable notes on what each still-failing file got stuck on."""
    notes: list[str] = []
    done = set(translator.done)
    latest: dict[str, V.VerifyReport] = {}
    for rep in translator.reports:
        latest[rep.path] = rep
    for f in translator.files:
        if str(f) in done:
            continue
        rep = latest.get(str(f))
        if rep is not None:
            notes.append(rep.summary())
    return notes


# --------------------------------------------------------------------------
# Run 2: Reflexion outer loop. Episodes re-attempt failing files with lessons.
# --------------------------------------------------------------------------

DECLARED_KEYS = ("translation",)


def _lesson_text(translator: Translator) -> str:
    """Build a concrete lesson from the files still failing verify."""
    lines: list[str] = []
    done = set(translator.done)
    for f in translator.files:
        if str(f) in done:
            continue
        rep = V.verify_file(f, run_tests=False)
        if rep.hits:
            h = rep.hits[0]
            lines.append(
                f"- {_rel(f)}: still has Japanese in a {h.kind} at line {h.line} "
                f"(e.g. \"{h.excerpt[:50]}\"). Translate EVERY comment and docstring, "
                f"including inline trailing comments."
            )
        elif not rep.parses_ok:
            lines.append(
                f"- {_rel(f)}: your edit broke Python syntax ({rep.detail}). "
                f"Re-read the file and translate text only, never code."
            )
        else:
            lines.append(
                f"- {_rel(f)}: module tests failed after your edit ({rep.detail}). "
                f"You changed behaviour; translate comments/docstrings only, never "
                f"code or string literals."
            )
    return "\n".join(lines)


def run_with_reflexion(
    translator: Translator,
    act: Callable[[dict[str, Any]], ActOutcome],
    *,
    log_path: Path,
    inner_max_iterations: int = 12,
    token_budget: int = 2_000_000,
    max_episodes: int = 3,
    epoch_len: int = 2,
    timeout: float = 6_000.0,
) -> RunResult:
    """Run the Reflexion outer loop; lessons from failed episodes feed the next.

    Uses the real :func:`~loop_agent.reflexion.run_reflexion` (two-signal model +
    RQGM epoch core), with ``propose_evaluator=None`` so the incumbent evaluator
    is fixed -- the ground truth here (parse + Japanese scan + pytest) is already
    non-gameable, so the evaluator-promotion safety core is a no-op for this task.
    """
    sink = JsonlEventSink(log_path)
    start = time.monotonic()
    token_acc = {"total": 0}

    def episode(ctx: Any) -> LoopResult:
        translator.lessons = ctx.memory_block
        conditions = [
            MaxIterations(inner_max_iterations),
            TokenBudget(token_budget),
            Timeout(timeout),
            GoalMet(lambda _s: translator.all_done()),
        ]
        inner_obs = LoopObserver(
            sinks=[sink], conditions=conditions, span_name="loop_agent.episode"
        )
        with inner_obs:
            run_id = f"self-translation-ep{ctx.episode}-{uuid.uuid4().hex[:8]}"
            store = LoopStore(
                connect(
                    REPO_ROOT
                    / "examples"
                    / "self_translation_poc"
                    / f".gate-{run_id}.sqlite3"
                )
            )
            result = run_gated_loop(
                act=act,
                verify=translator.verify,
                conditions=conditions,
                gather=translator.gather,
                on=lambda _ctx: True,
                resolver=_auto_approve,
                store=store,
                run_id=run_id,
                on_step=inner_obs.on_step,
            )
            inner_obs.record_result(result)
        token_acc["total"] += result.tokens_used
        return result

    def ground_truth(outcome: Any) -> GroundTruthSignal:
        frac = len(translator.done) / len(translator.files)
        return GroundTruthSignal(
            succeeded=outcome.succeeded and translator.all_done(),
            score=Score(ground_truth=frac, components={"translation": frac}),
            ground_truth_backed=True,
        )

    def reflect(
        history: tuple[StepRecord, ...],
        signal: GroundTruthSignal,
        _reward: float,
    ) -> Optional[Lesson]:
        if signal.succeeded or not history:
            return None
        text = _lesson_text(translator)
        if not text:
            return None
        return Lesson(
            text=text,
            episode=0,  # driver overwrites with the real episode number
            provenance=step_signature(history[-1]),  # grounded to a real step
            support=1.0,  # driver recomputes from grounding
        )

    evaluator = Evaluator(
        score=lambda o: Score(ground_truth=1.0 if getattr(o, "succeeded", False) else 0.0),
        name="translation-progress",
        rubric=DECLARED_KEYS,
    )
    held_out = HeldOut((Probe("held-noop", {"truth": 1.0}, gold_label=1.0),))

    result = run_observed_reflexion(
        episode=episode,
        ground_truth=ground_truth,
        reflect=reflect,
        evaluator=evaluator,
        convergence=[RubricThreshold(target=1.0, sustain=1), MaxEpisodes(max_episodes)],
        declared_keys=DECLARED_KEYS,
        production_tasks=["self-translation"],
        held_out=held_out,
        epoch_len=epoch_len,
        sinks=[sink],
    )
    elapsed = time.monotonic() - start

    done = list(translator.done)
    failed = [str(f) for f in translator.files if str(f) not in set(done)]
    return RunResult(
        label="reflexion",
        status=result.status,
        succeeded=result.succeeded and translator.all_done(),
        iterations=len(translator.reports),
        tokens=token_acc["total"],
        elapsed=elapsed,
        done=[_rel(Path(p)) for p in done],
        failed=[_rel(Path(p)) for p in failed],
        log_path=_rel(log_path),
        episodes=result.episodes,
        reason=result.reason,
        failure_notes=_collect_failure_notes(translator),
        episode_records=[
            {
                "episode": r.episode,
                "epoch": r.epoch,
                "gt_aggregate": round(r.gt_aggregate, 4),
                "reward": round(r.reward, 4),
                "admitted_lesson": r.admitted,
                "succeeded": r.succeeded,
                "detail": r.detail,
            }
            for r in result.state.episodes
        ],
    )


# --------------------------------------------------------------------------
# CLI.
# --------------------------------------------------------------------------


def _strip_japanese_stub(path: Path) -> None:
    """Test-only transform: blank Japanese in comments/docstrings to ASCII.

    Stands in for a real translation so the loop/gate/Reflexion mechanics can be
    exercised deterministically without claude. NOT used by --real.
    """
    import re

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out = []
    for ln in lines:
        out.append(re.sub(V._JAPANESE, "x", ln))
    path.write_text("".join(out), encoding="utf-8")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ and __doc__.splitlines()[0])
    parser.add_argument("--real", action="store_true", help="use the real ClaudeCodeAct (claude --print)")
    parser.add_argument("--selfcheck", action="store_true", help="fast local stub (no claude); validates wiring")
    parser.add_argument("--reflexion", action="store_true", help="run the Reflexion outer loop")
    parser.add_argument("--no-gate", action="store_true", help="disable the HumanGate (act-only)")
    parser.add_argument("--model", default="haiku", help="claude model alias (default: haiku)")
    parser.add_argument("--timeout", type=float, default=600.0, help="per-call timeout seconds")
    parser.add_argument("--log", default=None, help="JSONL log path")
    args = parser.parse_args(list(argv) if argv is not None else None)

    poc_dir = REPO_ROOT / "examples" / "self_translation_poc"
    label = "reflexion" if args.reflexion else "no_reflexion"
    log_path = Path(args.log) if args.log else poc_dir / f"run_{label}.jsonl"

    if args.selfcheck:
        act = make_stub_act(_strip_japanese_stub)
        run_tests = False
    elif args.real:
        act = build_real_act(model=args.model, timeout=args.timeout)
        run_tests = True
    else:
        parser.error("choose --real or --selfcheck")
        return 2

    translator = Translator(TARGET_FILES, run_tests=run_tests)

    if args.reflexion:
        res = run_with_reflexion(translator, act, log_path=log_path)
    else:
        res = run_no_reflexion(
            translator, act, log_path=log_path, gate=not args.no_gate
        )

    print("=== self-translation PoC ===")
    print(f"mode      : {res.label}{' (real)' if args.real else ' (selfcheck)'}")
    print(f"status    : {res.status} (succeeded={res.succeeded})")
    print(f"reason    : {res.reason}")
    print(f"iterations: {res.iterations}  episodes: {res.episodes}")
    print(f"tokens    : {res.tokens}")
    print(f"elapsed   : {res.elapsed:.1f}s")
    print(f"done      : {len(res.done)}/{len(translator.files)}")
    if res.failed:
        print(f"failed    : {res.failed}")
        for note in res.failure_notes:
            print(f"   - {note}")
    print(f"log       : {res.log_path}")
    return 0 if res.succeeded else 1


if __name__ == "__main__":
    raise SystemExit(main())
