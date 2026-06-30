"""loop-agent CLI launcher (Issue #31).

A thin, stdlib-only command line on top of the merged ``loop_agent`` package.
It turns a declarative TOML task definition into a live ``gather -> act ->
verify -> repeat`` run, persists every step to the state.db SoT
(:class:`loop_agent.store.DBProgressLog`), and lets you inspect / resume /
follow a run by its run-id.

Subcommands::

    loop-agent run ./task.toml [--max-iter N] [--token-budget N] [--timeout S]
    loop-agent status <run-id>
    loop-agent summary [--db PATH] [--limit N]
    loop-agent resume <run-id> ./task.toml
    loop-agent logs   <run-id> [--follow]
    loop-agent install-skills [--target-agent claude|codex|cursor|all] [--user | --target PATH]
    loop-agent                       # quick help + a sample task.toml

``act`` and ``verify`` each work in one of two modes (report.md R1):

1. ``command`` -- a subprocess. ``act`` runs the command (with ``{prompt}`` /
   ``{goal}`` / ``{iteration}`` substituted) and records its stdout as the
   observation; ``verify`` runs the command and treats exit-code 0 as the goal
   being met (ground truth = exit-code, exactly like
   :class:`loop_agent.demo.ExitCodeVerifier`).
2. ``python`` -- a ``module:attr`` reference to a Python callable used directly
   as the ``act`` / ``verify`` hook (the in-process seam the PoC drives with
   stubs; report.md S4.4).

The entry point is :func:`main`, wired in ``pyproject.toml`` as
``loop-agent = loop_agent.cli:main``.

Note on help strings: every user-facing string here stays ASCII (plain ``-``,
no em-dash) so ``--help`` does not crash under a cp932 console on Windows.
"""

from __future__ import annotations

import argparse
import importlib
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .conditions import MaxIterations, NoProgress, StopCondition, Timeout, TokenBudget
from .errors import ConfigError
from .events import JsonlEventSink
from .loop import ActOutcome, LoopResult, VerifyOutcome
from .observe import run_observed_loop
from .store import LoopStore, connect
from .store import DBProgressLog

# stdlib TOML reader (tomllib) ships in Python 3.11+. On 3.10 we fall back to
# the third-party ``tomli`` (same API) if present; otherwise TOML loading raises
# a clear, actionable error (see :func:`_read_toml`). The library itself keeps
# zero runtime dependencies -- this only affects the CLI's TOML mode on 3.10.
try:  # pragma: no cover - import path depends on the interpreter version
    import tomllib as _tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    try:
        import tomli as _tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover
        _tomllib = None  # type: ignore[assignment]

# Default state.db path when neither the TOML [state].db nor --db is given.
# A single db file holds many runs, keyed by run-id, so this is a sensible
# shared default for run/status/resume/logs in one working directory.
DEFAULT_DB = "loop-state.db"

# Fallback per-step subprocess timeout (seconds) when neither the hook nor a
# loop Timeout bounds it. A subprocess hook with no timeout can block forever,
# and the loop's stop conditions are only checked *between* iterations (an
# in-progress step is never interrupted; see conditions.Timeout), so an un-timed
# hang would defeat every cap and make the run unbounded. A finite default keeps
# each step -- and therefore the whole run -- bounded (report.md R3). Generous
# enough for real model calls; override per hook via [act]/[verify].timeout_seconds.
DEFAULT_SUBPROCESS_TIMEOUT = 3600.0

# Cap on how much subprocess stdout we keep as a step observation. Observations
# are persisted as JSON in state.db and key NoProgress; an unbounded blob would
# bloat the db and the no-progress signature for no benefit.
MAX_OBSERVATION_CHARS = 2000

# Poll interval (seconds) for `logs --follow` tailing the event journal.
FOLLOW_POLL_SECONDS = 0.5


# ``ConfigError`` の正準定義は loop_agent.errors にある (Issue #43)。CLI の TOML /
# 引数パースの設定エラーもこれを使い、:func:`main` が捕捉して stderr に出力し非ゼロ
# 終了する (想定外の内部エラーは traceback ごと伝播させる)。後方互換のため
# ``from loop_agent.cli import ConfigError`` は引き続き有効 (上の import で再公開)。

# -- TOML config -------------------------------------------------------------


@dataclass
class Config:
    """A parsed ``task.toml``: the goal, stop conditions, and act/verify hooks.

    Condition fields are ``None`` when absent from the TOML so that CLI flags
    can be layered on top with a clear "flag overrides file, file overrides
    nothing" precedence (:func:`build_conditions`). Exactly one of
    ``*_command`` / ``*_python`` is set for each of act and verify.
    """

    goal: str = ""
    run_id: Optional[str] = None
    db_path: Optional[str] = None
    events_path: Optional[str] = None

    # [conditions]
    max_iterations: Optional[int] = None
    token_budget: Optional[int] = None
    timeout_seconds: Optional[float] = None
    no_progress: Optional[tuple[int, int]] = None  # (window, repeat)

    # [act]
    act_command: Optional[list[str]] = None
    act_python: Optional[str] = None
    act_cost_per_step: int = 0
    act_timeout: Optional[float] = None

    # [verify]
    verify_command: Optional[list[str]] = None
    verify_python: Optional[str] = None
    verify_timeout: Optional[float] = None


def _read_toml(path: Path) -> dict[str, Any]:
    """Read and parse a TOML file into a dict, with friendly errors."""
    if _tomllib is None:  # pragma: no cover - only on 3.10 without tomli
        raise ConfigError(
            "reading TOML requires Python 3.11+ (stdlib tomllib) or the 'tomli' "
            "package on 3.10; install tomli or upgrade Python."
        )
    if not path.exists():
        raise ConfigError(f"task file not found: {path}")
    try:
        with path.open("rb") as fh:
            return _tomllib.load(fh)
    except (OSError, ValueError) as exc:
        # tomllib raises tomllib.TOMLDecodeError (a ValueError subclass).
        raise ConfigError(f"failed to parse {path}: {exc}") from exc


def _as_int(value: Any, where: str) -> int:
    # bool is an int subclass; reject it so `max_iterations = true` is not 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where} must be an integer, got {value!r}")
    return value


def _as_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where} must be a number, got {value!r}")
    return float(value)


def _as_str_list(value: Any, where: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{where} must be a list of strings, got {value!r}")
    if not value:
        raise ConfigError(f"{where} must be a non-empty list of strings")
    return list(value)


def _parse_hook(table: dict[str, Any], section: str) -> tuple[Optional[list[str]], Optional[str]]:
    """Validate an [act]/[verify] table into (command, python), exactly one set."""
    command = table.get("command")
    python = table.get("python")
    if command is not None and python is not None:
        raise ConfigError(
            f"[{section}] must set exactly one of 'command' or 'python', not both"
        )
    if command is None and python is None:
        raise ConfigError(f"[{section}] must set either 'command' or 'python'")
    if command is not None:
        return _as_str_list(command, f"[{section}].command"), None
    if not isinstance(python, str) or not python:
        raise ConfigError(f"[{section}].python must be a non-empty 'module:attr' string")
    return None, python


def _reject_unknown(table: dict[str, Any], known: set[str], section: str) -> None:
    """Reject typo'd / unrecognised keys so a misspelling is not silently dropped.

    A key like ``max_iteration`` (missing the trailing ``s``) would otherwise be
    ignored, quietly dropping the cap the user intended; fail loudly instead.
    """
    unknown = sorted(set(table) - known)
    if unknown:
        raise ConfigError(
            f"[{section}] has unknown key(s): {', '.join(unknown)}; "
            f"recognised keys are: {', '.join(sorted(known))}"
        )


_KNOWN_TOP_LEVEL = {"loop", "state", "conditions", "act", "verify"}


def parse_config(data: dict[str, Any]) -> Config:
    """Turn a parsed TOML mapping into a validated :class:`Config`.

    Split out from :func:`load_config` so it is unit-testable without a file.
    Unknown keys / tables are rejected (a typo is a usage error, not a silent
    no-op).
    """
    cfg = Config()
    _reject_unknown(data, _KNOWN_TOP_LEVEL, "<top level>")

    loop = data.get("loop", {})
    if not isinstance(loop, dict):
        raise ConfigError("[loop] must be a table")
    _reject_unknown(loop, {"goal", "run_id"}, "loop")
    goal = loop.get("goal", "")
    if not isinstance(goal, str):
        raise ConfigError("[loop].goal must be a string")
    cfg.goal = goal
    run_id = loop.get("run_id")
    if run_id is not None and (not isinstance(run_id, str) or not run_id):
        raise ConfigError("[loop].run_id must be a non-empty string when set")
    cfg.run_id = run_id

    state = data.get("state", {})
    if not isinstance(state, dict):
        raise ConfigError("[state] must be a table")
    _reject_unknown(state, {"db", "events"}, "state")
    db = state.get("db")
    if db is not None and not isinstance(db, str):
        raise ConfigError("[state].db must be a string path")
    cfg.db_path = db
    events = state.get("events")
    if events is not None and not isinstance(events, str):
        raise ConfigError("[state].events must be a string path")
    cfg.events_path = events

    conditions = data.get("conditions", {})
    if not isinstance(conditions, dict):
        raise ConfigError("[conditions] must be a table")
    _reject_unknown(
        conditions,
        {"max_iterations", "token_budget", "timeout_seconds", "no_progress"},
        "conditions",
    )
    if "max_iterations" in conditions:
        cfg.max_iterations = _as_int(
            conditions["max_iterations"], "[conditions].max_iterations"
        )
    if "token_budget" in conditions:
        cfg.token_budget = _as_int(
            conditions["token_budget"], "[conditions].token_budget"
        )
    if "timeout_seconds" in conditions:
        cfg.timeout_seconds = _as_number(
            conditions["timeout_seconds"], "[conditions].timeout_seconds"
        )
    if "no_progress" in conditions:
        np = conditions["no_progress"]
        if not isinstance(np, dict) or "window" not in np or "repeat" not in np:
            raise ConfigError(
                "[conditions].no_progress must be a table with 'window' and 'repeat'"
            )
        _reject_unknown(np, {"window", "repeat"}, "conditions.no_progress")
        cfg.no_progress = (
            _as_int(np["window"], "[conditions].no_progress.window"),
            _as_int(np["repeat"], "[conditions].no_progress.repeat"),
        )

    act = data.get("act")
    if not isinstance(act, dict):
        raise ConfigError("[act] table is required")
    _reject_unknown(act, {"command", "python", "cost_per_step", "timeout_seconds"}, "act")
    cfg.act_command, cfg.act_python = _parse_hook(act, "act")
    if "cost_per_step" in act:
        cfg.act_cost_per_step = _as_int(act["cost_per_step"], "[act].cost_per_step")
    if "timeout_seconds" in act:
        cfg.act_timeout = _as_number(act["timeout_seconds"], "[act].timeout_seconds")

    verify = data.get("verify")
    if not isinstance(verify, dict):
        raise ConfigError("[verify] table is required")
    _reject_unknown(verify, {"command", "python", "timeout_seconds"}, "verify")
    cfg.verify_command, cfg.verify_python = _parse_hook(verify, "verify")
    if "timeout_seconds" in verify:
        cfg.verify_timeout = _as_number(
            verify["timeout_seconds"], "[verify].timeout_seconds"
        )

    # The goal fills {prompt}/{goal} in a subprocess act command; an empty goal
    # there would silently send empty prompts. Require it when it is actually
    # interpolated. (Python-callable act ignores the goal, so an empty goal is
    # legitimate there and is not rejected.)
    if cfg.act_command is not None and not cfg.goal:
        interpolates = any(
            "{prompt}" in arg or "{goal}" in arg for arg in cfg.act_command
        )
        if interpolates:
            raise ConfigError(
                "[loop].goal must be a non-empty string when [act].command "
                "interpolates {prompt}/{goal}"
            )

    return cfg


def load_config(path: str | Path) -> Config:
    """Read ``path`` and return a validated :class:`Config`."""
    return parse_config(_read_toml(Path(path)))


# -- hook construction -------------------------------------------------------


def resolve_callable(spec: str) -> Callable[..., Any]:
    """Import a ``module:attr`` (or ``module.attr``) reference to a callable.

    Used for the Python-callable act/verify mode. Raises :class:`ConfigError`
    with an actionable message when the module / attribute is missing or the
    target is not callable.
    """
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
    elif "." in spec:
        module_name, _, attr = spec.rpartition(".")
    else:
        raise ConfigError(
            f"callable reference {spec!r} must be 'module:attr' (or 'module.attr')"
        )
    if not module_name or not attr:
        raise ConfigError(f"callable reference {spec!r} must be 'module:attr'")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"cannot import module {module_name!r}: {exc}") from exc
    try:
        target = getattr(module, attr)
    except AttributeError as exc:
        raise ConfigError(
            f"module {module_name!r} has no attribute {attr!r}"
        ) from exc
    if not callable(target):
        raise ConfigError(f"{spec!r} is not callable")
    return target


def _substitute(arg: str, *, goal: str, iteration: int) -> str:
    """Substitute the placeholders supported in act command arguments."""
    return (
        arg.replace("{prompt}", goal)
        .replace("{goal}", goal)
        .replace("{iteration}", str(iteration))
    )


def _effective_timeout(
    explicit: Optional[float], loop_timeout: Optional[float]
) -> float:
    """Pick a *finite* subprocess timeout: explicit, else loop cap, else default.

    A subprocess hook must never run un-timed (an infinite hang defeats every
    between-iteration cap; see :data:`DEFAULT_SUBPROCESS_TIMEOUT`).
    """
    if explicit is not None:
        return explicit
    if loop_timeout is not None:
        return loop_timeout
    return DEFAULT_SUBPROCESS_TIMEOUT


def build_act(
    cfg: Config, *, loop_timeout: Optional[float] = None
) -> Callable[[Any], ActOutcome]:
    """Build the ``act`` hook from the config (subprocess or Python callable).

    A subprocess ``act`` always gets a finite timeout (``[act].timeout_seconds``
    if set, else the loop ``timeout_seconds``, else
    :data:`DEFAULT_SUBPROCESS_TIMEOUT`) so a hung command cannot make the run
    unbounded.
    """
    if cfg.act_python is not None:
        return resolve_callable(cfg.act_python)

    command = cfg.act_command
    assert command is not None  # guaranteed by _parse_hook
    goal = cfg.goal
    cost = cfg.act_cost_per_step
    timeout = _effective_timeout(cfg.act_timeout, loop_timeout)

    def act(context: Any) -> ActOutcome:
        iteration = getattr(context, "iteration", 0)
        argv = [_substitute(a, goal=goal, iteration=iteration) for a in command]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return ActOutcome(
                observation=f"act timed out ({timeout:g}s)", tokens=cost
            )
        except (FileNotFoundError, OSError) as exc:
            # A missing / unspawnable command must not crash the run with a raw
            # traceback: record it as a (zero-progress) observation so the loop
            # keeps its termination contract and the run row is finalised.
            return ActOutcome(
                observation=f"act command failed to start ({command[0]!r}): {exc}",
                tokens=cost,
            )
        observation = (proc.stdout or "").strip() or f"exit={proc.returncode}"
        if len(observation) > MAX_OBSERVATION_CHARS:
            observation = observation[:MAX_OBSERVATION_CHARS] + "...(truncated)"
        return ActOutcome(observation=observation, tokens=cost)

    return act


def build_verify(
    cfg: Config, *, loop_timeout: Optional[float] = None
) -> Callable[[ActOutcome], VerifyOutcome]:
    """Build the ``verify`` hook from the config (subprocess or Python callable).

    The subprocess verifier uses exit-code 0 as ground truth (report.md R1),
    mirroring :class:`loop_agent.demo.ExitCodeVerifier` but command-agnostic. It
    always gets a finite timeout (see :func:`build_act`) so a hung verify command
    cannot make the run unbounded.
    """
    if cfg.verify_python is not None:
        return resolve_callable(cfg.verify_python)

    command = cfg.verify_command
    assert command is not None  # guaranteed by _parse_hook
    timeout = _effective_timeout(cfg.verify_timeout, loop_timeout)

    def verify(_outcome: ActOutcome) -> VerifyOutcome:
        try:
            proc = subprocess.run(
                command, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return VerifyOutcome(
                goal_met=False, detail=f"red (timeout {timeout:g}s)"
            )
        except (FileNotFoundError, OSError) as exc:
            # A missing / unspawnable verify command is not a met goal: treat it
            # as red so the loop terminates cleanly via its caps rather than
            # crashing with a traceback and leaving the run row non-terminal.
            return VerifyOutcome(
                goal_met=False, detail=f"red (command {command[0]!r} failed: {exc})"
            )
        green = proc.returncode == 0
        detail = "green" if green else f"red (exit={proc.returncode})"
        return VerifyOutcome(goal_met=green, detail=detail)

    return verify


def build_conditions(
    cfg: Config,
    *,
    max_iter: Optional[int] = None,
    token_budget: Optional[int] = None,
    timeout: Optional[float] = None,
) -> list[StopCondition]:
    """Compose stop conditions from the config, with CLI flags taking priority.

    Precedence per cap is "CLI flag, else TOML value, else absent". The result
    must include at least one cap that is *guaranteed to fire* so a run can never
    loop forever (report.md R3); otherwise :class:`ConfigError` is raised. The
    ``verify`` hook drives natural goal termination, so no explicit GoalMet
    condition is added.

    Not all caps are guaranteed: ``MaxIterations`` / ``Timeout`` always fire
    eventually (iteration and elapsed monotonically grow), but ``TokenBudget``
    only fires if tokens strictly increase -- which, for a subprocess ``act``,
    needs ``cost_per_step > 0`` -- and ``NoProgress`` only fires if an action
    recurs. A config whose only cap can never advance (e.g. ``token_budget`` with
    a subprocess act reporting 0 tokens/step, the documented default) is rejected.
    """
    mi = max_iter if max_iter is not None else cfg.max_iterations
    tb = token_budget if token_budget is not None else cfg.token_budget
    to = timeout if timeout is not None else cfg.timeout_seconds

    conditions: list[StopCondition] = []
    try:
        if mi is not None:
            conditions.append(MaxIterations(mi))
        if tb is not None:
            conditions.append(TokenBudget(tb))
        if to is not None:
            conditions.append(Timeout(to))
        if cfg.no_progress is not None:
            window, repeat = cfg.no_progress
            conditions.append(NoProgress(window=window, repeat=repeat))
    except ValueError as exc:
        # Surface the dataclass __post_init__ validation as a usage error.
        raise ConfigError(str(exc)) from exc

    if not conditions:
        raise ConfigError(
            "no stop condition configured: set at least one of "
            "[conditions].max_iterations / token_budget / timeout_seconds / "
            "no_progress (or pass --max-iter / --token-budget / --timeout) so "
            "the loop is bounded."
        )

    # R3: at least one cap must be *guaranteed* to terminate the loop.
    guaranteed = mi is not None or to is not None
    if not guaranteed and tb is not None:
        # TokenBudget terminates only if tokens strictly increase each step. For
        # a subprocess act that means cost_per_step > 0; a Python-callable act
        # may charge tokens itself, so we accept it (caller's responsibility).
        if cfg.act_command is not None and cfg.act_cost_per_step <= 0:
            raise ConfigError(
                "token_budget is the only terminating cap but the subprocess "
                "[act].command reports 0 tokens/step (cost_per_step=0), so it can "
                "never fire and the loop would be unbounded; set [act].cost_per_step "
                "> 0, or add [conditions].max_iterations / timeout_seconds."
            )
        guaranteed = True
    if not guaranteed:
        # Only NoProgress remains, which may never fire (it depends on an action
        # recurring); R3 needs a hard bound.
        raise ConfigError(
            "the configured stop condition(s) are not guaranteed to terminate "
            "(no_progress only fires if an action repeats); add "
            "[conditions].max_iterations or timeout_seconds to bound the loop."
        )
    return conditions


def generate_run_id() -> str:
    """Generate a fresh, human-legible run-id for a new run."""
    return f"run-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"


def _require_existing_db(db_path: str) -> None:
    """Fail cleanly for status/resume/logs when the db file does not exist.

    Avoids :func:`~loop_agent.store.connect` creating an empty db (+ WAL/-shm)
    on disk just to report "no such run".
    """
    if not Path(db_path).exists():
        raise ConfigError(f"state.db not found: {db_path}")


# -- subcommands -------------------------------------------------------------


def _execute_loop(
    cfg: Config,
    run_id: str,
    db_path: str,
    *,
    max_iter: Optional[int],
    token_budget: Optional[int],
    timeout: Optional[float],
    out: Any,
) -> LoopResult:
    """Shared run/resume body: wire SoT + observer and drive the loop.

    The :class:`~loop_agent.store.DBProgressLog` is both the step sink and the
    resume seed: its ``state`` is the reconstructed :class:`LoopState` (empty for
    a new run-id, the persisted mid-run state for an existing one), so the exact
    same wiring serves a fresh run and a resume.
    """
    conditions = build_conditions(
        cfg, max_iter=max_iter, token_budget=token_budget, timeout=timeout
    )
    # The effective loop Timeout (flag over TOML) bounds each subprocess hook
    # when it has no explicit timeout, so a single step cannot outlast the run.
    loop_timeout = timeout if timeout is not None else cfg.timeout_seconds
    act = build_act(cfg, loop_timeout=loop_timeout)
    verify = build_verify(cfg, loop_timeout=loop_timeout)
    sinks = [JsonlEventSink(cfg.events_path)] if cfg.events_path else []

    db = DBProgressLog(db_path, run_id)
    try:
        # Resuming a previously terminal run (status stopped/goal_met, ended_at
        # set) must reopen it so the row reflects active work: otherwise status()
        # reports a finished run mid-resume, and a crash before record_result
        # would leave the DB falsely terminal with newly-appended steps. Reset to
        # running and drop the stale stop_reason; record_result re-finalizes at
        # the end. For a fresh run this matches nothing (no-op).
        _reopen_run(db.store, run_id)
        result = run_observed_loop(
            act=act,
            verify=verify,
            conditions=conditions,
            sinks=sinks,
            on_step=db.on_step,
            initial_state=db.state,
        )
        db.record_result(result)
    finally:
        db.close()

    _print_result(run_id, db_path, result, out)
    return result


def _reopen_run(store: LoopStore, run_id: str) -> None:
    """Reset a terminal run row to ``running`` before a resume appends steps."""
    with store.transaction():
        store.conn.execute(
            "UPDATE run SET status = 'running', ended_at = NULL, "
            "updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE run_id = ? AND status != 'running'",
            (run_id,),
        )
        store.conn.execute("DELETE FROM stop_reason WHERE run_id = ?", (run_id,))


def _print_result(run_id: str, db_path: str, result: LoopResult, out: Any) -> None:
    print(f"run-id     : {run_id}", file=out)
    print(f"db         : {db_path}", file=out)
    print(f"status     : {result.status}", file=out)
    print(f"reason     : {result.reason}", file=out)
    print(f"iterations : {result.iterations}", file=out)
    print(f"tokens     : {result.tokens_used}", file=out)
    print(f"elapsed    : {result.elapsed:.3f}s", file=out)


def cmd_run(args: argparse.Namespace, out: Any = None) -> int:
    out = sys.stdout if out is None else out
    cfg = load_config(args.task)
    db_path = args.db or cfg.db_path or DEFAULT_DB
    explicit_run_id = args.run_id or cfg.run_id
    # An explicit run-id that already exists would silently *resume* (counters
    # carry over), making `run` and `resume` indistinguishable. Refuse it so the
    # user picks `resume` or a fresh id. Auto-generated ids never collide.
    if explicit_run_id and Path(db_path).exists():
        conn = connect(db_path)
        try:
            if LoopStore(conn).get_run(explicit_run_id) is not None:
                raise ConfigError(
                    f"run {explicit_run_id!r} already exists in {db_path}; use "
                    "'resume' to continue it or choose a different --run-id"
                )
        finally:
            conn.close()
    run_id = explicit_run_id or generate_run_id()
    result = _execute_loop(
        cfg,
        run_id,
        db_path,
        max_iter=args.max_iter,
        token_budget=args.token_budget,
        timeout=args.timeout,
        out=out,
    )
    return 0 if result.succeeded else 1


def cmd_resume(args: argparse.Namespace, out: Any = None) -> int:
    out = sys.stdout if out is None else out
    cfg = load_config(args.task)
    db_path = args.db or cfg.db_path or DEFAULT_DB
    # Resume only makes sense for a run that already exists in the db.
    _require_existing_db(db_path)
    conn = connect(db_path)
    try:
        store = LoopStore(conn)
        if store.get_run(args.run_id) is None:
            raise ConfigError(
                f"no run {args.run_id!r} found in {db_path}; use 'run' to start one"
            )
    finally:
        conn.close()
    print(f"resuming {args.run_id} from {db_path}", file=out)
    result = _execute_loop(
        cfg,
        args.run_id,
        db_path,
        max_iter=args.max_iter,
        token_budget=args.token_budget,
        timeout=args.timeout,
        out=out,
    )
    return 0 if result.succeeded else 1


def cmd_status(args: argparse.Namespace, out: Any = None) -> int:
    out = sys.stdout if out is None else out
    db_path = args.db or DEFAULT_DB
    _require_existing_db(db_path)
    conn = connect(db_path)
    try:
        store = LoopStore(conn)
        run = store.get_run(args.run_id)
        if run is None:
            raise ConfigError(f"no run {args.run_id!r} found in {db_path}")
        stop = store.get_stop_reason(args.run_id)
        steps = store.read_steps(args.run_id)
        pending = store.list_pending_decisions(args.run_id)
    finally:
        conn.close()

    print(f"run-id     : {run['run_id']}", file=out)
    print(f"status     : {run['status']}", file=out)
    print(f"goal_met   : {bool(run['goal_met'])}", file=out)
    print(f"iterations : {run['iterations']} ({len(steps)} steps recorded)", file=out)
    print(f"tokens     : {run['tokens_used']}", file=out)
    print(f"elapsed    : {run['elapsed']:.3f}s", file=out)
    print(f"started_at : {run['started_at']}", file=out)
    print(f"updated_at : {run['updated_at']}", file=out)
    print(f"ended_at   : {run['ended_at'] or '-'}", file=out)
    if stop is not None:
        print(f"stop       : {stop['name'] or '-'} ({stop['reason']})", file=out)
    if pending:
        print(f"pending    : {len(pending)} decision(s) awaiting a human", file=out)
    return 0


def cmd_summary(args: argparse.Namespace, out: Any = None) -> int:
    """Print a compact read-only summary of runs in state.db."""
    out = sys.stdout if out is None else out
    db_path = args.db or DEFAULT_DB
    _require_existing_db(db_path)
    if args.limit < 1:
        raise ConfigError("--limit must be >= 1")
    conn = connect(db_path)
    try:
        limit = args.limit
        rows = conn.execute(
            "SELECT run_id, status, goal_met, iterations, tokens_used, elapsed, "
            "started_at, updated_at, ended_at FROM run "
            "ORDER BY updated_at DESC, started_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        if not rows:
            print(f"db         : {db_path}", file=out)
            print("runs       : 0", file=out)
            return 0
        pending_counts = {
            row["run_id"]: row["count"]
            for row in conn.execute(
                "SELECT run_id, COUNT(*) AS count FROM pending_decision "
                "WHERE status = 'pending' GROUP BY run_id"
            ).fetchall()
        }
        stop_reasons = {
            row["run_id"]: row
            for row in conn.execute(
                "SELECT run_id, name, reason FROM stop_reason"
            ).fetchall()
        }
        event_counts = {
            row["run_id"]: row["count"]
            for row in conn.execute(
                "SELECT run_id, COUNT(*) AS count FROM event GROUP BY run_id"
            ).fetchall()
        }
    finally:
        conn.close()

    print(f"db         : {db_path}", file=out)
    print(f"runs       : {len(rows)}", file=out)
    print(
        "run-id                         status     iter  tokens   elapsed  pending  events  stop",
        file=out,
    )
    for row in rows:
        run_id = row["run_id"]
        stop = stop_reasons.get(run_id)
        stop_text = "-"
        if stop is not None:
            stop_text = stop["name"] or "-"
            if stop["reason"]:
                stop_text = f"{stop_text}: {stop['reason']}"
        print(
            f"{run_id:<30} {row['status']:<10} {row['iterations']:>4} "
            f"{row['tokens_used']:>7} {row['elapsed']:>8.3f} "
            f"{pending_counts.get(run_id, 0):>7} {event_counts.get(run_id, 0):>7} "
            f"{stop_text}",
            file=out,
        )
    return 0


def _format_event(event: dict[str, Any]) -> str:
    payload = {k: v for k, v in event["payload"].items()}
    return f"{event['occurred_at']}  {event['kind']:<10}  {payload}"


def _run_is_running(store: LoopStore, run_id: str) -> bool:
    run = store.get_run(run_id)
    return run is not None and run["status"] == "running"


def cmd_logs(args: argparse.Namespace, out: Any = None) -> int:
    out = sys.stdout if out is None else out
    db_path = args.db or DEFAULT_DB
    _require_existing_db(db_path)
    conn = connect(db_path)
    try:
        store = LoopStore(conn)
        if store.get_run(args.run_id) is None:
            raise ConfigError(f"no run {args.run_id!r} found in {db_path}")

        events = store.read_events(args.run_id)
        for event in events:
            print(_format_event(event), file=out)

        if not args.follow:
            return 0

        # Follow: poll for new events until the run row leaves 'running' (the
        # terminal signal -- record_result flips status and writes loop_end in
        # one transaction). Keying on the *run status* rather than on seeing a
        # loop_end event is what makes follow work for a resumed run, whose
        # event table still holds the prior leg's loop_end. Track how many
        # events we have already printed so we only emit new ones.
        seen = len(events)
        try:
            while _run_is_running(store, args.run_id):
                time.sleep(FOLLOW_POLL_SECONDS)
                events = store.read_events(args.run_id)
                for event in events[seen:]:
                    print(_format_event(event), file=out)
                seen = len(events)
            # Flush any final events (incl. the closing loop_end) committed
            # together with the terminal status between the last poll and now.
            events = store.read_events(args.run_id)
            for event in events[seen:]:
                print(_format_event(event), file=out)
        except KeyboardInterrupt:  # pragma: no cover - interactive only
            print("\n(stopped following)", file=out)
    finally:
        conn.close()
    return 0


# -- install-skills ----------------------------------------------------------

# The reference-bundled coding-agent skill (Issue #73) ships *inside* the
# package at ``loop_agent/skills/loop-agent/`` so ``pip install loop-agent``
# carries it. ``install-skills`` copies it into the selected coding agent's
# skill discovery directory. The library code itself never imports these files;
# they are docs-for-the-agent, distributed with the library so the agent always
# sees the version that matches the installed loop-agent.
SKILL_NAME = "loop-agent"
SKILL_TARGET_CLAUDE = "claude"
SKILL_TARGET_CODEX = "codex"
SKILL_TARGET_CURSOR = "cursor"
SKILL_TARGET_ALL = "all"
SKILL_TARGETS = (SKILL_TARGET_CLAUDE, SKILL_TARGET_CODEX, SKILL_TARGET_CURSOR)
SKILL_TARGET_CHOICES = (*SKILL_TARGETS, SKILL_TARGET_ALL)


def _bundled_skill_dir() -> Path:
    """Filesystem path to the bundled skill shipped inside the package.

    ``cli.py`` lives at ``loop_agent/cli.py``, so the skill is a sibling
    ``skills/loop-agent/`` directory. This resolves correctly for both an
    editable checkout and an installed wheel (both lay the package out as real
    files on disk).
    """
    return Path(__file__).resolve().parent / "skills" / SKILL_NAME


def _skill_dest_for_agent(agent: str, base: Path) -> Path:
    """Return the default skill destination for one coding-agent surface."""
    if agent == SKILL_TARGET_CLAUDE:
        return base / ".claude" / "skills" / SKILL_NAME
    if agent == SKILL_TARGET_CODEX:
        return base / ".codex" / "skills" / SKILL_NAME
    if agent == SKILL_TARGET_CURSOR:
        # Cursor's built-in skills live under ~/.cursor/skills-cursor/ and are
        # managed by Cursor itself. Personal/project skills belong in skills/.
        return base / ".cursor" / "skills" / SKILL_NAME
    raise ConfigError(
        f"unknown skill target agent {agent!r}; expected one of {SKILL_TARGET_CHOICES}"
    )


def _resolve_skill_dest(args: argparse.Namespace) -> Path:
    """Pick the install destination for the selected target agent.

    ``--target`` is an exact destination directory and preserves the original
    Claude-oriented behaviour. Without ``--target``, the selected target agent's
    project-local or user-global skills directory is used.
    """
    if getattr(args, "target", None) is not None:
        return Path(args.target).expanduser()
    base = Path.home() if getattr(args, "user", False) else Path.cwd()
    agent = getattr(args, "target_agent", SKILL_TARGET_CLAUDE)
    if agent == SKILL_TARGET_ALL:
        raise ConfigError("--target-agent all cannot be resolved to a single path")
    return _skill_dest_for_agent(agent, base)


def _resolve_skill_dests(args: argparse.Namespace) -> list[tuple[str, Path]]:
    """Return all install destinations requested by the CLI args."""
    agent = getattr(args, "target_agent", SKILL_TARGET_CLAUDE)
    if getattr(args, "target", None) is not None:
        if agent == SKILL_TARGET_ALL:
            raise ConfigError("--target cannot be combined with --target-agent all")
        return [(agent, Path(args.target).expanduser())]
    base = Path.home() if getattr(args, "user", False) else Path.cwd()
    agents = SKILL_TARGETS if agent == SKILL_TARGET_ALL else (agent,)
    return [(a, _skill_dest_for_agent(a, base)) for a in agents]

def _installed_skill_name(skill_md: Path) -> Optional[str]:
    """Return the ``name:`` from a SKILL.md YAML frontmatter, or None.

    Used to recognise a *prior loop-agent install* at the destination (so a
    reinstall may safely replace it) without mistaking a different skill's
    directory for one of ours.
    """
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    front = text[:end] if end != -1 else text
    for line in front.splitlines():
        if line.startswith("name:"):
            return line[len("name:") :].strip().strip("\"'")
    return None


def _install_skill_tree(source: Path, dest: Path) -> int:
    """Install the bundled skill tree to ``dest`` and return copied file count."""
    if dest.exists():
        if not dest.is_dir():
            raise ConfigError(
                f"install destination {dest} exists and is not a directory"
            )
        # Replace the whole tree so the install *converges* to the bundled
        # contents: a plain merge-copy would leave behind references that a newer
        # loop-agent has renamed or removed, so a coding agent could keep reading
        # stale files. Guard the footgun: only wipe a directory that is empty or
        # is *our own* prior install (a SKILL.md whose frontmatter name is
        # loop-agent). Refuse a non-empty unrelated directory or a *different*
        # skill's directory (a mis-pointed --target) so we never delete it.
        if any(dest.iterdir()) and _installed_skill_name(dest / "SKILL.md") != SKILL_NAME:
            raise ConfigError(
                f"{dest} is not empty and is not a loop-agent skill install "
                "(no SKILL.md with 'name: loop-agent'); refusing to overwrite it. "
                "Point --target at an empty or dedicated directory."
            )
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Fresh copy of the bundle. Re-running is idempotent: an existing install is
    # removed above, so the second run lands the same tree as the first.
    shutil.copytree(source, dest)
    return sum(1 for p in dest.rglob("*") if p.is_file())


def cmd_install_skills(args: argparse.Namespace, out: Any = None) -> int:
    out = sys.stdout if out is None else out
    source = _bundled_skill_dir()
    if not source.is_dir():
        # A wheel that somehow dropped the data files would land here; fail with
        # an actionable message rather than silently installing nothing.
        raise ConfigError(
            f"bundled skill not found at {source}; the installed loop-agent "
            "package appears to be missing its skill data files."
        )
    for agent, dest in _resolve_skill_dests(args):
        file_count = _install_skill_tree(source, dest)
        print(
            f"installed loop-agent skill for {agent} -> {dest} ({file_count} files)",
            file=out,
        )
    print(
        "restart your coding agent to pick up the skill.",
        file=out,
    )
    return 0

# -- argparse wiring ---------------------------------------------------------


SAMPLE_TASK_TOML = """\
[loop]
goal = "make the test suite pass"
# run_id = "my-run"          # optional; auto-generated when omitted

[conditions]
max_iterations = 20
token_budget = 500000
timeout_seconds = 3600
# no_progress = { window = 5, repeat = 3 }   # optional: stop when stuck

[act]
# subprocess mode: {prompt}/{goal}/{iteration} are substituted per step
command = ["claude", "--print", "{prompt}"]
cost_per_step = 0            # tokens charged per step (for token_budget)
# python = "mypkg.hooks:act"  # OR in-process Python callable: act(context)

[verify]
# subprocess mode: exit-code 0 == goal met
command = ["pytest", "-q"]
# python = "mypkg.hooks:verify"  # OR callable: verify(outcome) -> VerifyOutcome

[state]
# db = "loop-state.db"       # optional; defaults to loop-state.db
# events = "events.jsonl"    # optional JSONL event journal
"""

QUICK_HELP = """\
loop-agent - run a bounded gather->act->verify loop from a TOML task file.

Usage:
  loop-agent run ./task.toml [--max-iter N] [--token-budget N] [--timeout S]
  loop-agent status <run-id> [--db PATH]
  loop-agent summary [--db PATH] [--limit N]
  loop-agent resume <run-id> ./task.toml [--db PATH]
  loop-agent logs   <run-id> [--follow] [--db PATH]
  loop-agent install-skills [--target-agent claude|codex|cursor|all] [--user | --target PATH]

Run 'loop-agent <command> --help' for per-command options.

Sample task.toml:

"""


def _add_db_flag(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        default=None,
        help=f"state.db path (default: {DEFAULT_DB}, or [state].db from the TOML)",
    )


def _add_override_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-iter",
        type=int,
        default=None,
        dest="max_iter",
        help="override [conditions].max_iterations",
    )
    parser.add_argument(
        "--token-budget",
        type=int,
        default=None,
        dest="token_budget",
        help="override [conditions].token_budget",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="override [conditions].timeout_seconds (seconds)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loop-agent",
        description="Run a bounded gather->act->verify loop from a TOML task file.",
    )
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="start a loop from a TOML task file")
    p_run.add_argument("task", help="path to the task.toml")
    p_run.add_argument(
        "--run-id", default=None, dest="run_id", help="run-id (default: auto-generated)"
    )
    _add_db_flag(p_run)
    _add_override_flags(p_run)
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="show a run's progress from state.db")
    p_status.add_argument("run_id", help="the run-id to inspect")
    _add_db_flag(p_status)
    p_status.set_defaults(func=cmd_status)

    p_summary = sub.add_parser("summary", help="show a read-only run summary from state.db")
    _add_db_flag(p_summary)
    p_summary.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum runs to show (default: 20)",
    )
    p_summary.set_defaults(func=cmd_summary)

    p_resume = sub.add_parser("resume", help="resume an interrupted run")
    p_resume.add_argument("run_id", help="the run-id to resume")
    p_resume.add_argument("task", help="path to the same task.toml")
    _add_db_flag(p_resume)
    _add_override_flags(p_resume)
    p_resume.set_defaults(func=cmd_resume)

    p_logs = sub.add_parser("logs", help="show a run's event journal")
    p_logs.add_argument("run_id", help="the run-id whose events to show")
    p_logs.add_argument(
        "--follow", action="store_true", help="keep printing new events until the run ends"
    )
    _add_db_flag(p_logs)
    p_logs.set_defaults(func=cmd_logs)

    p_install = sub.add_parser(
        "install-skills",
        help="copy the bundled loop-agent coding-agent skill into a skills dir",
    )
    p_install.add_argument(
        "--target-agent",
        choices=SKILL_TARGET_CHOICES,
        default=SKILL_TARGET_CLAUDE,
        help="coding-agent skill surface to install for (default: claude)",
    )
    dest = p_install.add_mutually_exclusive_group()
    dest.add_argument(
        "--user",
        action="store_true",
        help="install into the selected agent's user-global skills directory "
        "instead of the project-local skills directory",
    )
    dest.add_argument(
        "--target",
        default=None,
        help="install into this exact directory instead of the selected agent's "
        "default skills directory",
    )
    p_install.set_defaults(func=cmd_install_skills)

    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point. Returns a process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if getattr(args, "command", None) is None:
        # No subcommand: print quick help plus a sample task.toml.
        print(QUICK_HELP, end="")
        print(SAMPLE_TASK_TOML)
        return 0

    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
