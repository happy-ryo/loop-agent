"""Tests for the loop-agent CLI launcher (Issue #31).

Covers TOML parsing/validation, stop-condition composition with flag overrides,
act/verify construction in both subprocess and Python-callable modes, the
``module:attr`` resolver, and the run/status/resume/logs subcommands end to end
(driven through :func:`loop_agent.cli.main` against a temporary state.db).

Subprocess hooks invoke ``sys.executable -c ...`` rather than shell builtins so
the suite is portable across platforms.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from loop_agent import ActOutcome, LoopState, StepRecord, VerifyOutcome
from loop_agent.cli import (
    Config,
    ConfigError,
    build_act,
    build_conditions,
    build_verify,
    generate_run_id,
    load_config,
    main,
    parse_config,
    resolve_callable,
)
from loop_agent.conditions import MaxIterations, NoProgress, Timeout, TokenBudget
from loop_agent.store import LoopStore, connect

# -- Python-callable hooks used by the callable-mode tests -------------------
# Defined at module scope so they are importable as "test_cli:act_ok" etc.

_VERIFY_CALLS = {"n": 0}


def act_ok(_context):
    return ActOutcome(observation="callable-act", tokens=5)


def verify_after_two(_outcome):
    _VERIFY_CALLS["n"] += 1
    met = _VERIFY_CALLS["n"] >= 2
    return VerifyOutcome(goal_met=met, detail="done" if met else "not yet")


not_callable = 123  # for the "not callable" resolver test

# Counting hooks used by the resume test to pin that resume *continues* from the
# persisted iteration rather than restarting from zero.
_RESUME_ACT_CALLS = {"n": 0}


def counting_act(_context):
    _RESUME_ACT_CALLS["n"] += 1
    return ActOutcome(observation=f"step-{_RESUME_ACT_CALLS['n']}", tokens=0)


def verify_never(_outcome):
    return VerifyOutcome(goal_met=False, detail="not yet")


# -- helpers -----------------------------------------------------------------


def _py(code: str) -> list[str]:
    """A subprocess command list running inline Python (portable)."""
    return [sys.executable, "-c", code]


def write_toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def toml_literal(value: str) -> str:
    """Return a TOML literal string, suitable for Windows paths."""
    return "'" + value.replace("'", "''") + "'"


# -- TOML parsing / validation ----------------------------------------------


def test_parse_config_full():
    data = {
        "loop": {"goal": "g", "run_id": "r1"},
        "conditions": {
            "max_iterations": 10,
            "token_budget": 1000,
            "timeout_seconds": 60,
            "no_progress": {"window": 5, "repeat": 3},
        },
        "act": {"command": ["echo", "hi"], "cost_per_step": 4},
        "verify": {"command": ["pytest"]},
        "state": {"db": "x.db", "events": "e.jsonl"},
    }
    cfg = parse_config(data)
    assert cfg.goal == "g"
    assert cfg.run_id == "r1"
    assert cfg.max_iterations == 10
    assert cfg.token_budget == 1000
    assert cfg.timeout_seconds == 60.0
    assert cfg.no_progress == (5, 3)
    assert cfg.act_command == ["echo", "hi"]
    assert cfg.act_cost_per_step == 4
    assert cfg.verify_command == ["pytest"]
    assert cfg.db_path == "x.db"
    assert cfg.events_path == "e.jsonl"


def test_parse_config_python_mode():
    data = {
        "loop": {"goal": "g"},
        "conditions": {"max_iterations": 3},
        "act": {"python": "pkg:act"},
        "verify": {"python": "pkg:verify"},
    }
    cfg = parse_config(data)
    assert cfg.act_python == "pkg:act"
    assert cfg.verify_python == "pkg:verify"
    assert cfg.act_command is None


def test_parse_config_requires_act_and_verify():
    with pytest.raises(ConfigError, match=r"\[act\] table is required"):
        parse_config({"loop": {"goal": "g"}, "verify": {"command": ["x"]}})
    with pytest.raises(ConfigError, match=r"\[verify\] table is required"):
        parse_config({"loop": {"goal": "g"}, "act": {"command": ["x"]}})


def test_parse_config_hook_exactly_one_mode():
    base = {"loop": {"goal": "g"}, "verify": {"command": ["x"]}}
    with pytest.raises(ConfigError, match="exactly one"):
        parse_config({**base, "act": {"command": ["x"], "python": "p:a"}})
    with pytest.raises(ConfigError, match="either 'command' or 'python'"):
        parse_config({**base, "act": {}})


def test_parse_config_rejects_bad_types():
    base = {"loop": {"goal": "g"}, "act": {"command": ["x"]}, "verify": {"command": ["y"]}}
    with pytest.raises(ConfigError, match="must be an integer"):
        parse_config({**base, "conditions": {"max_iterations": "ten"}})
    # bool must not pass as int
    with pytest.raises(ConfigError, match="must be an integer"):
        parse_config({**base, "conditions": {"max_iterations": True}})
    with pytest.raises(ConfigError, match="must be a number"):
        parse_config({**base, "conditions": {"timeout_seconds": "soon"}})
    with pytest.raises(ConfigError, match="list of strings"):
        parse_config({"loop": {"goal": "g"}, "act": {"command": [1, 2]},
                      "verify": {"command": ["y"]}})


def test_parse_config_no_progress_requires_fields():
    base = {"loop": {"goal": "g"}, "act": {"command": ["x"]}, "verify": {"command": ["y"]}}
    with pytest.raises(ConfigError, match="no_progress"):
        parse_config({**base, "conditions": {"no_progress": {"window": 5}}})


def test_parse_config_rejects_unknown_keys():
    base = {"loop": {"goal": "g"}, "act": {"command": ["x"]}, "verify": {"command": ["y"]}}
    # typo'd cap key must not be silently dropped
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({**base, "conditions": {"max_iteration": 5}})
    # unknown top-level table
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({**base, "conditionz": {}})
    # unknown key in [loop]
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({**base, "loop": {"goal": "g", "extra": 1}})


def test_parse_config_requires_goal_for_interpolation():
    # empty goal + a command that interpolates {prompt} -> rejected
    with pytest.raises(ConfigError, match="goal must be a non-empty"):
        parse_config({
            "loop": {"goal": ""},
            "conditions": {"max_iterations": 1},
            "act": {"command": ["claude", "{prompt}"]},
            "verify": {"command": ["true"]},
        })
    # empty goal but no interpolation -> allowed
    cfg = parse_config({
        "loop": {"goal": ""},
        "conditions": {"max_iterations": 1},
        "act": {"command": ["echo", "hi"]},
        "verify": {"command": ["true"]},
    })
    assert cfg.goal == ""
    # empty goal in python mode -> allowed (goal is not interpolated)
    cfg = parse_config({
        "loop": {"goal": ""},
        "conditions": {"max_iterations": 1},
        "act": {"python": "p:a"},
        "verify": {"python": "p:v"},
    })
    assert cfg.act_python == "p:a"


def test_load_config_from_file(tmp_path):
    toml = write_toml(
        tmp_path / "t.toml",
        '[loop]\ngoal="g"\n[conditions]\nmax_iterations=4\n'
        '[act]\ncommand=["echo","x"]\n[verify]\ncommand=["true"]\n',
    )
    cfg = load_config(toml)
    assert cfg.goal == "g"
    assert cfg.max_iterations == 4


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="task file not found"):
        load_config(tmp_path / "nope.toml")


def test_load_config_bad_toml(tmp_path):
    bad = write_toml(tmp_path / "bad.toml", "this is = not = valid = toml")
    with pytest.raises(ConfigError, match="failed to parse"):
        load_config(bad)


# -- stop conditions ---------------------------------------------------------


def test_build_conditions_from_config():
    cfg = Config(max_iterations=5, token_budget=100, timeout_seconds=30,
                 no_progress=(4, 2))
    conds = build_conditions(cfg)
    kinds = {type(c) for c in conds}
    assert kinds == {MaxIterations, TokenBudget, Timeout, NoProgress}


def test_build_conditions_flag_overrides_toml():
    cfg = Config(max_iterations=5, token_budget=100, timeout_seconds=30)
    conds = build_conditions(cfg, max_iter=99, token_budget=7, timeout=1.5)
    by_type = {type(c): c for c in conds}
    assert by_type[MaxIterations].limit == 99
    assert by_type[TokenBudget].budget == 7
    assert by_type[Timeout].seconds == 1.5


def test_build_conditions_requires_at_least_one():
    with pytest.raises(ConfigError, match="no stop condition configured"):
        build_conditions(Config())


def test_build_conditions_rejects_token_only_zero_cost_command():
    # token_budget is the only cap but a subprocess act charges 0 tokens/step ->
    # it can never fire -> unbounded -> reject (R3).
    cfg = Config(token_budget=100, act_command=["echo", "x"], act_cost_per_step=0)
    with pytest.raises(ConfigError, match="never fire"):
        build_conditions(cfg)


def test_build_conditions_allows_token_only_with_positive_cost():
    cfg = Config(token_budget=100, act_command=["echo", "x"], act_cost_per_step=5)
    conds = build_conditions(cfg)
    assert {type(c) for c in conds} == {TokenBudget}


def test_build_conditions_allows_token_only_python_act():
    # a Python-callable act may charge tokens itself; accept token-only there.
    cfg = Config(token_budget=100, act_python="pkg:act")
    conds = build_conditions(cfg)
    assert {type(c) for c in conds} == {TokenBudget}


def test_build_conditions_rejects_no_progress_only():
    cfg = Config(no_progress=(5, 3), act_command=["echo", "x"])
    with pytest.raises(ConfigError, match="not guaranteed to terminate"):
        build_conditions(cfg)


def test_build_conditions_token_zero_cost_ok_with_hard_cap():
    # a hard cap rescues a 0-token token_budget config.
    cfg = Config(max_iterations=3, token_budget=100, act_command=["echo", "x"])
    conds = build_conditions(cfg)
    assert {type(c) for c in conds} == {MaxIterations, TokenBudget}


def test_build_conditions_surfaces_validation_error():
    with pytest.raises(ConfigError, match="MaxIterations limit must be >= 0"):
        build_conditions(Config(), max_iter=-1)


# -- callable resolver -------------------------------------------------------


def test_resolve_callable_colon():
    fn = resolve_callable("test_cli:act_ok")
    assert fn is act_ok


def test_resolve_callable_dotted():
    fn = resolve_callable("test_cli.act_ok")
    assert fn is act_ok


def test_resolve_callable_errors():
    with pytest.raises(ConfigError, match="module:attr"):
        resolve_callable("noseparator")
    with pytest.raises(ConfigError, match="cannot import module"):
        resolve_callable("no_such_module_xyz:foo")
    with pytest.raises(ConfigError, match="no attribute"):
        resolve_callable("test_cli:does_not_exist")
    with pytest.raises(ConfigError, match="not callable"):
        resolve_callable("test_cli:not_callable")


# -- act / verify hooks ------------------------------------------------------


def test_build_act_subprocess_substitutes_and_costs():
    cfg = Config(goal="GOAL", act_command=_py("import sys; print(sys.argv[1])"),
                 act_cost_per_step=3)
    # add the placeholder as a trailing arg to observe substitution
    cfg.act_command = cfg.act_command + ["{prompt}|{iteration}"]
    act = build_act(cfg)

    class Ctx:
        iteration = 4

    outcome = act(Ctx())
    assert outcome.observation == "GOAL|4"
    assert outcome.tokens == 3


def test_build_act_subprocess_truncates_long_output():
    from loop_agent.cli import MAX_OBSERVATION_CHARS

    cfg = Config(goal="g", act_command=_py(f"print('a' * {MAX_OBSERVATION_CHARS + 50})"))
    outcome = build_act(cfg)(object())
    assert outcome.observation.endswith("...(truncated)")
    assert len(outcome.observation) <= MAX_OBSERVATION_CHARS + len("...(truncated)")


def test_build_act_python_mode():
    cfg = Config(act_python="test_cli:act_ok")
    outcome = build_act(cfg)(object())
    assert outcome.observation == "callable-act"
    assert outcome.tokens == 5


def test_build_verify_subprocess_green_and_red():
    green = build_verify(Config(verify_command=_py("import sys; sys.exit(0)")))
    red = build_verify(Config(verify_command=_py("import sys; sys.exit(1)")))
    assert green(ActOutcome()).goal_met is True
    out = red(ActOutcome())
    assert out.goal_met is False
    assert "exit=1" in out.detail


def test_build_verify_subprocess_timeout():
    cfg = Config(verify_command=_py("import time; time.sleep(5)"), verify_timeout=0.2)
    out = build_verify(cfg)(ActOutcome())
    assert out.goal_met is False
    assert "timeout" in out.detail


def test_effective_timeout_precedence():
    from loop_agent.cli import DEFAULT_SUBPROCESS_TIMEOUT, _effective_timeout

    assert _effective_timeout(12.0, 99.0) == 12.0           # explicit wins
    assert _effective_timeout(None, 99.0) == 99.0           # else loop timeout
    assert _effective_timeout(None, None) == DEFAULT_SUBPROCESS_TIMEOUT  # else default


def test_build_act_missing_command_does_not_crash():
    cfg = Config(goal="g", act_command=["definitely-not-a-real-binary-xyz"])
    outcome = build_act(cfg)(object())  # must not raise FileNotFoundError
    assert "failed to start" in outcome.observation


def test_build_verify_missing_command_is_red():
    cfg = Config(verify_command=["definitely-not-a-real-binary-xyz"])
    out = build_verify(cfg)(ActOutcome())  # must not raise FileNotFoundError
    assert out.goal_met is False
    assert "failed" in out.detail


# -- end-to-end subcommands --------------------------------------------------


def _run_toml(tmp_path: Path, *, run_id: str, db: str, max_iter: int,
              verify_exit: int) -> Path:
    act = 'command=[%s, "-c", "print(1)"]' % repr(sys.executable)
    verify = 'command=[%s, "-c", "import sys; sys.exit(%d)"]' % (
        repr(sys.executable), verify_exit)
    return write_toml(
        tmp_path / f"{run_id}.toml",
        f'[loop]\ngoal="g"\nrun_id="{run_id}"\n'
        f'[conditions]\nmax_iterations={max_iter}\n'
        f'[act]\n{act}\n[verify]\n{verify}\n'
        f"[state]\ndb={toml_literal(db)}\n",
    )


def test_cmd_run_goal_met(tmp_path, capsys):
    db = str(tmp_path / "run.db")
    toml = _run_toml(tmp_path, run_id="ok", db=db, max_iter=5, verify_exit=0)
    rc = main(["run", str(toml)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status     : goal_met" in out
    # persisted to state.db
    conn = connect(db)
    try:
        run = LoopStore(conn).get_run("ok")
    finally:
        conn.close()
    assert run is not None and run["status"] == "goal_met"


def test_cmd_run_stopped_by_cap(tmp_path, capsys):
    db = str(tmp_path / "cap.db")
    toml = _run_toml(tmp_path, run_id="cap", db=db, max_iter=2, verify_exit=1)
    rc = main(["run", str(toml)])
    out = capsys.readouterr().out
    assert rc == 1  # not a success -> non-zero exit
    assert "status     : stopped" in out
    assert "max iterations (2/2)" in out


def test_cmd_run_flag_override(tmp_path, capsys):
    db = str(tmp_path / "ovr.db")
    toml = _run_toml(tmp_path, run_id="ovr", db=db, max_iter=2, verify_exit=1)
    main(["run", str(toml), "--max-iter", "3"])
    out = capsys.readouterr().out
    assert "max iterations (3/3)" in out


def test_cmd_run_python_callable(tmp_path, capsys):
    _VERIFY_CALLS["n"] = 0  # reset shared counter
    db = str(tmp_path / "py.db")
    toml = write_toml(
        tmp_path / "py.toml",
        '[loop]\ngoal="g"\nrun_id="py"\n[conditions]\nmax_iterations=10\n'
        '[act]\npython="test_cli:act_ok"\n'
        '[verify]\npython="test_cli:verify_after_two"\n'
        f"[state]\ndb={toml_literal(db)}\n",
    )
    rc = main(["run", str(toml)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "status     : goal_met" in out
    assert "iterations : 2" in out
    assert "tokens     : 10" in out  # 5 tokens x 2 steps


def test_cmd_status(tmp_path, capsys):
    db = str(tmp_path / "s.db")
    toml = _run_toml(tmp_path, run_id="s1", db=db, max_iter=2, verify_exit=1)
    main(["run", str(toml)])
    capsys.readouterr()  # discard run output
    rc = main(["status", "s1", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "run-id     : s1" in out
    assert "status     : stopped" in out
    assert "2 steps recorded" in out


def test_cmd_status_missing_run(tmp_path, capsys):
    # db exists (a different run was recorded) but the asked-for run does not.
    db = str(tmp_path / "s2.db")
    main(["run", str(_run_toml(tmp_path, run_id="real", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    rc = main(["status", "ghost", "--db", db])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no run 'ghost'" in err


def test_cmd_status_missing_db(tmp_path, capsys):
    rc = main(["status", "ghost", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "state.db not found" in capsys.readouterr().err
    # The clean error must NOT have created an empty db file as a side effect.
    assert not (tmp_path / "nope.db").exists()


def test_cmd_summary(tmp_path, capsys):
    db = str(tmp_path / "summary.db")
    main(["run", str(_run_toml(tmp_path, run_id="s1", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    main(["run", str(_run_toml(tmp_path, run_id="s2", db=db, max_iter=1,
                               verify_exit=0))])
    capsys.readouterr()

    rc = main(["summary", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "runs       : 2" in out
    assert "s1" in out and "stopped" in out
    assert "s2" in out and "goal_met" in out
    assert "events" in out


def test_cmd_summary_empty_db(tmp_path, capsys):
    db = tmp_path / "empty.db"
    conn = connect(db)
    conn.close()
    rc = main(["summary", "--db", str(db)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "runs       : 0" in out


def test_cmd_summary_missing_db_and_bad_limit(tmp_path, capsys):
    rc = main(["summary", "--db", str(tmp_path / "nope.db")])
    assert rc == 2
    assert "state.db not found" in capsys.readouterr().err

    db = tmp_path / "bad-limit.db"
    conn = connect(db)
    conn.close()
    rc = main(["summary", "--db", str(db), "--limit", "0"])
    assert rc == 2
    assert "--limit must be >= 1" in capsys.readouterr().err


def _seed_spiky_run(db: str) -> None:
    store = LoopStore(connect(db))
    try:
        state = store.load_or_init("spiky")
        rows = [
            StepRecord(0, "a", tokens=10, goal_met=False),
            StepRecord(1, "b", tokens=10, goal_met=False),
            StepRecord(2, "c", tokens=50, goal_met=False),
        ]
        for record, elapsed, tokens_used in zip(rows, [1.0, 2.0, 8.0], [10, 20, 70]):
            state = LoopState(
                iteration=record.iteration + 1,
                tokens_used=tokens_used,
                elapsed=elapsed,
                goal_met=False,
                history=[*state.history, record],
            )
            store.record_step("spiky", record, state)
    finally:
        store.conn.close()


def test_cmd_spikes(tmp_path, capsys):
    db = str(tmp_path / "spikes.db")
    _seed_spiky_run(db)

    rc = main([
        "spikes",
        "spiky",
        "--db",
        db,
        "--token-window",
        "2",
        "--latency-window",
        "2",
        "--multiplier",
        "3",
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "spikes     : 2" in out
    assert "spike=token" in out
    assert "spike=latency" in out


def test_cmd_spikes_missing_run(tmp_path, capsys):
    db = str(tmp_path / "spikes-missing.db")
    _seed_spiky_run(db)
    rc = main(["spikes", "ghost", "--db", db])
    assert rc == 2
    assert "no run 'ghost'" in capsys.readouterr().err


def test_cmd_dashboard(tmp_path, capsys):
    db = str(tmp_path / "dashboard.db")
    _seed_spiky_run(db)
    output = tmp_path / "dashboard.html"

    rc = main(["dashboard", "--db", db, "--output", str(output)])
    out = capsys.readouterr().out
    html = output.read_text(encoding="utf-8")
    assert rc == 0
    assert "dashboard  :" in out
    assert "runs       : 1" in out
    assert "loop-agent operations dashboard" in html
    assert "spiky" in html
    assert "Steps: spiky" in html


def test_cmd_dashboard_empty_db(tmp_path, capsys):
    db = tmp_path / "dashboard-empty.db"
    conn = connect(db)
    conn.close()
    output = tmp_path / "empty.html"
    rc = main(["dashboard", "--db", str(db), "--output", str(output)])
    html = output.read_text(encoding="utf-8")
    assert rc == 0
    assert "runs       : 0" in capsys.readouterr().out
    assert "loop-agent operations dashboard" in html


def test_cmd_resume_continues(tmp_path, capsys):
    # Pin that resume *continues* from the persisted iteration rather than
    # restarting at 0. A counting act lets us tell the two apart: the iteration
    # count alone cannot (a fresh restart up to the same cap lands on the same
    # number). After run(cap=2) act ran twice; resume(cap=4) must add exactly two
    # more calls (iterations 2,3), not four (a fresh 0..3 -> 6 total).
    _RESUME_ACT_CALLS["n"] = 0
    db = str(tmp_path / "res.db")
    toml = write_toml(
        tmp_path / "res.toml",
        '[loop]\ngoal="g"\nrun_id="r"\n[conditions]\nmax_iterations=2\n'
        '[act]\npython="test_cli:counting_act"\n'
        '[verify]\npython="test_cli:verify_never"\n'
        f"[state]\ndb={toml_literal(db)}\n",
    )
    assert main(["run", str(toml)]) == 1
    capsys.readouterr()
    assert _RESUME_ACT_CALLS["n"] == 2  # iterations 0, 1

    rc = main(["resume", "r", str(toml), "--db", db, "--max-iter", "4"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "resuming r" in out
    assert "iterations : 4" in out
    assert _RESUME_ACT_CALLS["n"] == 4  # only iterations 2, 3 were added


def test_cmd_resume_missing_run(tmp_path, capsys):
    db = str(tmp_path / "res2.db")
    # db exists with a different run; resuming a non-existent run-id errors.
    main(["run", str(_run_toml(tmp_path, run_id="x", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    toml = _run_toml(tmp_path, run_id="x", db=db, max_iter=2, verify_exit=1)
    rc = main(["resume", "ghost", str(toml), "--db", db])
    err = capsys.readouterr().err
    assert rc == 2
    assert "no run 'ghost'" in err


def test_resume_reopens_terminal_run(tmp_path, capsys):
    # After a stopped run, resume must reopen the row to 'running' (and drop the
    # stale stop_reason) so status is not falsely terminal mid-resume.
    from loop_agent.cli import _reopen_run

    db = str(tmp_path / "reopen.db")
    main(["run", str(_run_toml(tmp_path, run_id="t", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    conn = connect(db)
    try:
        store = LoopStore(conn)
        assert store.get_run("t")["status"] == "stopped"
        assert store.get_stop_reason("t") is not None
        _reopen_run(store, "t")
        assert store.get_run("t")["status"] == "running"
        assert store.get_run("t")["ended_at"] is None
        assert store.get_stop_reason("t") is None
    finally:
        conn.close()


def test_cmd_run_rejects_existing_run_id(tmp_path, capsys):
    db = str(tmp_path / "dup.db")
    toml = _run_toml(tmp_path, run_id="dup", db=db, max_iter=1, verify_exit=1)
    assert main(["run", str(toml)]) == 1
    capsys.readouterr()
    # A second `run` with the same explicit run-id must refuse (not silent-resume).
    rc = main(["run", str(toml)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "already exists" in err


def test_cmd_logs(tmp_path, capsys):
    db = str(tmp_path / "l.db")
    toml = _run_toml(tmp_path, run_id="lg", db=db, max_iter=1, verify_exit=1)
    main(["run", str(toml)])
    capsys.readouterr()
    rc = main(["logs", "lg", "--db", db])
    out = capsys.readouterr().out
    assert rc == 0
    assert "loop_begin" in out
    assert "loop_step" in out
    assert "loop_end" in out


def test_cmd_logs_follow_terminal_run_exits(tmp_path, capsys):
    # --follow on an already-terminal run must print events and exit promptly
    # (not hang and not be confused by the historical loop_end).
    db = str(tmp_path / "lf.db")
    main(["run", str(_run_toml(tmp_path, run_id="lf", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    rc = main(["logs", "lf", "--db", db, "--follow"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "loop_end" in out


def test_run_is_running_discriminator(tmp_path, capsys):
    # The follow terminal-check keys on run status, which is what makes follow
    # work for a resumed run (whose event table still has the prior loop_end).
    from loop_agent.cli import _reopen_run, _run_is_running

    db = str(tmp_path / "rr.db")
    main(["run", str(_run_toml(tmp_path, run_id="rr", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    conn = connect(db)
    try:
        store = LoopStore(conn)
        assert _run_is_running(store, "rr") is False  # stopped
        _reopen_run(store, "rr")
        assert _run_is_running(store, "rr") is True  # reopened for resume
    finally:
        conn.close()


def test_cmd_logs_missing_run(tmp_path, capsys):
    db = str(tmp_path / "l2.db")
    main(["run", str(_run_toml(tmp_path, run_id="real", db=db, max_iter=1,
                               verify_exit=1))])
    capsys.readouterr()
    rc = main(["logs", "ghost", "--db", db])
    assert rc == 2
    assert "no run 'ghost'" in capsys.readouterr().err


# -- init-harness scaffold ---------------------------------------------------


def test_cmd_init_harness_light_template(tmp_path, capsys):
    out_dir = tmp_path / "light-harness"

    rc = main(["init-harness", "--template", "light", "--output", str(out_dir)])
    out = capsys.readouterr().out

    assert rc == 0
    assert "template   : light" in out
    harness = out_dir / "harness.py"
    readme = out_dir / "README.md"
    assert harness.exists()
    assert readme.exists()
    text = harness.read_text(encoding="utf-8")
    assert "ActOutcome" in text
    assert "VerifyOutcome" in text
    assert "MaxIterations(5)" in text


@pytest.mark.parametrize(
    ("template", "needle"),
    [
        ("claude", "ClaudeCodeAct"),
        ("codex", "CodexAct"),
    ],
)
def test_cmd_init_harness_agent_templates(tmp_path, capsys, template, needle):
    out_dir = tmp_path / template

    rc = main(["init-harness", "--template", template, "--output", str(out_dir)])
    capsys.readouterr()

    assert rc == 0
    text = (out_dir / "harness.py").read_text(encoding="utf-8")
    assert needle in text
    assert "PytestVerifier" in text
    assert "TokenBudget" in text


def test_cmd_init_harness_refuses_overwrite_without_force(tmp_path, capsys):
    out_dir = tmp_path / "harness"
    assert main(["init-harness", "--output", str(out_dir)]) == 0
    capsys.readouterr()

    rc = main(["init-harness", "--output", str(out_dir)])
    err = capsys.readouterr().err

    assert rc == 2
    assert "refusing to overwrite" in err

    rc = main(["init-harness", "--output", str(out_dir), "--force"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "created    :" in out
# -- top-level main ----------------------------------------------------------


def test_main_no_args_prints_sample(capsys):
    rc = main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "loop-agent" in out
    assert "[loop]" in out  # the sample task.toml is shown


def test_main_run_missing_file(tmp_path, capsys):
    rc = main(["run", str(tmp_path / "nope.toml")])
    assert rc == 2
    assert "task file not found" in capsys.readouterr().err


def test_generate_run_id_is_unique():
    assert generate_run_id() != generate_run_id()
    assert generate_run_id().startswith("run-")

