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
    SeamTimeout,
    StateError,
    UnsupportedTimeoutKill,
    run_loop,
)
from loop_agent.conditions import GoalMet, MaxIterations
from loop_agent.errors import AsyncSeamInSyncLoop as _ErrAsync
from loop_agent.errors import ConfigError as _ErrConfig
from loop_agent.errors import SeamTimeout as _ErrSeamTimeout
from loop_agent.errors import UnsupportedTimeoutKill as _ErrUnsupportedKill


# -- 1. hierarchy shape ----------------------------------------------------


def test_loop_error_is_exception():
    assert issubclass(LoopError, Exception)


def test_all_subtypes_derive_from_loop_error():
    for sub in (
        ConfigError,
        StateError,
        AsyncSeamInSyncLoop,
        SeamTimeout,
        UnsupportedTimeoutKill,
    ):
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


def test_seam_timeout_hierarchy_and_builtin_bases():
    # Issue #71: SeamTimeout is relocated under StateError (a kill-mode per-call
    # timeout = a run-time invariant violation). It was a bare Exception before,
    # so the StateError compat bases (ValueError / RuntimeError) only *widen*
    # what catches it -- `except SeamTimeout` is unaffected.
    assert issubclass(SeamTimeout, StateError)
    assert issubclass(SeamTimeout, LoopError)
    assert issubclass(SeamTimeout, RuntimeError)
    assert issubclass(SeamTimeout, ValueError)
    # ...but it is NOT a config error (a distinct, run-time-state failure).
    assert not issubclass(SeamTimeout, ConfigError)


def test_seam_timeout_constructs_with_unchanged_attributes():
    # Behaviour-preserving relocation (Issue #71 scope): the `seam` / `seconds`
    # attributes and the message are exactly as #42 established them.
    exc = SeamTimeout("act", 1.5)
    assert exc.seam == "act"
    assert exc.seconds == 1.5
    assert "act" in str(exc) and "hard kill" in str(exc)


def test_unsupported_timeout_kill_hierarchy_and_builtin_bases():
    # Issue #71: UnsupportedTimeoutKill is relocated under ConfigError (a seam/
    # platform combination misconfigured for hard kill), so it gains the
    # ConfigError surface (ValueError / TypeError). It ALSO keeps RuntimeError as
    # an explicit base because it used to be a bare RuntimeError -- pre-#71
    # `except RuntimeError` callers must keep catching it.
    assert issubclass(UnsupportedTimeoutKill, ConfigError)
    assert issubclass(UnsupportedTimeoutKill, LoopError)
    assert issubclass(UnsupportedTimeoutKill, RuntimeError)  # back-compat base
    assert issubclass(UnsupportedTimeoutKill, ValueError)
    assert issubclass(UnsupportedTimeoutKill, TypeError)
    # ...but it is NOT a state error (it is an up-front config/env refusal).
    assert not issubclass(UnsupportedTimeoutKill, StateError)


def test_timeout_exceptions_are_distinct_from_each_other():
    # The two relocated types must stay tellable apart (one is run-time state,
    # the other is up-front config), so neither over-catches the other.
    assert not issubclass(SeamTimeout, UnsupportedTimeoutKill)
    assert not issubclass(UnsupportedTimeoutKill, SeamTimeout)


def test_mro_is_well_formed():
    # Multiple inheritance must linearise cleanly (no MRO conflict at import).
    for sub in (
        ConfigError,
        StateError,
        AsyncSeamInSyncLoop,
        SeamTimeout,
        UnsupportedTimeoutKill,
    ):
        assert sub.__mro__[0] is sub
        assert sub.__mro__[1] is LoopError or sub.__mro__[1] in (
            StateError,
            ConfigError,
        )


# -- 2. canonical identity across re-export sites --------------------------


def test_config_error_canonical_across_modules():
    # cli.py and the top-level package re-export the same class object.
    assert ConfigError is _ErrConfig
    assert loop_agent.cli.ConfigError is ConfigError


def test_async_seam_canonical_across_modules():
    assert AsyncSeamInSyncLoop is _ErrAsync
    assert loop_agent._async.AsyncSeamInSyncLoop is AsyncSeamInSyncLoop


def test_timeout_exceptions_canonical_across_modules():
    # Issue #71: the canonical home is loop_agent.errors; loop.py and the
    # top-level package re-export the *same* class objects (so the raise site in
    # loop.py and an `except loop_agent.SeamTimeout` catch one identity).
    import loop_agent.loop  # ensure the re-exporting module is loaded

    assert SeamTimeout is _ErrSeamTimeout
    assert loop_agent.loop.SeamTimeout is SeamTimeout
    assert UnsupportedTimeoutKill is _ErrUnsupportedKill
    assert loop_agent.loop.UnsupportedTimeoutKill is UnsupportedTimeoutKill


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
