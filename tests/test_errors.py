"""The unified LoopError hierarchy (Issue #43).

Covers the shape of the hierarchy, the backwards-compatibility builtin bases,
that real raise paths produce the right type, and that error chains (``raise ...
from ...``) preserve the cause so the origin stays traceable.
"""

import pytest

import loop_agent
import loop_agent.cli  # ensure the cli submodule is loaded for the alias check below
from loop_agent import (
    AsyncSeamInSyncLoop,
    ConfigError,
    LoopError,
    StateError,
    run_loop,
)
from loop_agent.conditions import GoalMet, MaxIterations
from loop_agent.errors import AsyncSeamInSyncLoop as _ErrAsync
from loop_agent.errors import ConfigError as _ErrConfig


# -- 1. hierarchy shape ----------------------------------------------------


def test_loop_error_is_exception():
    assert issubclass(LoopError, Exception)


def test_all_subtypes_derive_from_loop_error():
    for sub in (ConfigError, StateError, AsyncSeamInSyncLoop):
        assert issubclass(sub, LoopError)


def test_config_error_builtin_bases():
    # Backwards compat: pre-hierarchy callers `except ValueError` / `except
    # TypeError` the validation sites that now raise ConfigError.
    assert issubclass(ConfigError, ValueError)
    assert issubclass(ConfigError, TypeError)


def test_state_error_builtin_bases():
    # Store/gate/loop state-machine sites used to raise a bare ValueError;
    # loop's defensive driver guard a bare RuntimeError. Keep both catchable.
    assert issubclass(StateError, ValueError)
    assert issubclass(StateError, RuntimeError)


def test_async_seam_builtin_base():
    assert issubclass(AsyncSeamInSyncLoop, RuntimeError)
    # ...and it is NOT a ValueError (it is a distinct, non-config error).
    assert not issubclass(AsyncSeamInSyncLoop, ValueError)


def test_mro_is_well_formed():
    # Multiple inheritance must linearise cleanly (no MRO conflict at import).
    for sub in (ConfigError, StateError, AsyncSeamInSyncLoop):
        assert sub.__mro__[0] is sub
        assert sub.__mro__[1] is LoopError


# -- 2. canonical identity across re-export sites --------------------------


def test_config_error_canonical_across_modules():
    # cli.py and the top-level package re-export the same class object.
    assert ConfigError is _ErrConfig
    assert loop_agent.cli.ConfigError is ConfigError


def test_async_seam_canonical_across_modules():
    assert AsyncSeamInSyncLoop is _ErrAsync
    assert loop_agent._async.AsyncSeamInSyncLoop is AsyncSeamInSyncLoop


# -- 3. real raise paths produce the right type ----------------------------


def test_config_error_raised_by_validation_and_catchable_three_ways():
    # MaxIterations(-1) validates its argument at construction.
    with pytest.raises(ConfigError):
        MaxIterations(-1)
    # The same raise is catchable as the base and as the compat builtin.
    with pytest.raises(LoopError):
        MaxIterations(-1)
    with pytest.raises(ValueError):
        MaxIterations(-1)


def test_explicit_type_check_raises_config_error():
    # The library's *explicit* type validation (here: `conditions` is neither an
    # AnyOf nor a sequence) is a ConfigError, catchable as LoopError / TypeError.
    bad = dict(
        act=lambda ctx: loop_agent.ActOutcome(observation="x", tokens=0),
        verify=lambda o: loop_agent.VerifyOutcome(goal_met=True),
        conditions=42,
    )
    with pytest.raises(ConfigError):
        run_loop(**bad)
    with pytest.raises(LoopError):
        run_loop(**bad)
    with pytest.raises(TypeError):
        run_loop(**bad)


def test_incidental_type_error_is_not_wrapped():
    # Documented boundary: passing a type-hint-violating value to an un-checked
    # numeric path surfaces as a plain TypeError (NOT a LoopError). This pins the
    # contract in docs/errors.md so it cannot silently drift.
    with pytest.raises(TypeError) as exc:
        MaxIterations(None)  # `None < 0` raises before any explicit validation
    assert not isinstance(exc.value, LoopError)


def test_async_seam_raised_by_sync_loop_with_async_act():
    async def async_act(_ctx):  # an awaitable seam in the synchronous driver
        return loop_agent.ActOutcome(observation="x", tokens=0)

    with pytest.raises(AsyncSeamInSyncLoop):
        run_loop(
            act=async_act,
            verify=lambda o: loop_agent.VerifyOutcome(goal_met=True),
            conditions=[MaxIterations(3)],
        )
    # Backwards compat: the same path is catchable as RuntimeError and LoopError.
    with pytest.raises(RuntimeError):
        run_loop(
            act=async_act,
            verify=lambda o: loop_agent.VerifyOutcome(goal_met=True),
            conditions=[MaxIterations(3)],
        )
    with pytest.raises(LoopError):
        run_loop(
            act=async_act,
            verify=lambda o: loop_agent.VerifyOutcome(goal_met=True),
            conditions=[MaxIterations(3)],
        )


def test_unknown_gate_disposition_raises_state_error():
    # A gate returning an unrecognised disposition is a runtime protocol
    # violation -> StateError (still catchable as ValueError for compat).
    class BadGate:
        def review(self, context, state):
            return loop_agent.GateReview(disposition="not-a-real-disposition")

    kwargs = dict(
        act=lambda ctx: loop_agent.ActOutcome(observation="x", tokens=0),
        verify=lambda o: loop_agent.VerifyOutcome(goal_met=True),
        conditions=[MaxIterations(3)],
        gate=BadGate(),
    )
    with pytest.raises(StateError):
        run_loop(**kwargs)
    with pytest.raises(LoopError):
        run_loop(**kwargs)
    with pytest.raises(ValueError):
        run_loop(**kwargs)


# -- 4. error chains stay traceable ----------------------------------------


def test_payload_validation_preserves_cause():
    # transport._dumps_payload translates the underlying TypeError into a
    # ConfigError via `raise ... from exc`, so the origin stays traceable.
    from loop_agent.transport import _dumps_payload

    with pytest.raises(ConfigError) as exc:
        _dumps_payload({"bad": object()})
    assert exc.value.__cause__ is not None
    assert isinstance(exc.value.__cause__, TypeError)


# -- 5. precise subtypes do not over-catch ---------------------------------


def test_config_error_and_state_error_are_distinct():
    # A ConfigError must not be caught as StateError and vice versa, so callers
    # can tell "bad input" from "bad runtime state" apart.
    assert not issubclass(ConfigError, StateError)
    assert not issubclass(StateError, ConfigError)


def test_goal_met_stop_condition_is_unaffected():
    # Sanity: a normal (non-error) control-flow type is untouched by the refactor.
    assert not issubclass(GoalMet, LoopError)
