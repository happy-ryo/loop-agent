"""The unified exception hierarchy for loop_agent (Issue #43).

Every error raised *by this library* derives from :class:`LoopError`, so a
caller can catch the whole surface in one place::

    from loop_agent import LoopError

    try:
        run_loop(...)
    except LoopError as exc:
        ...  # any loop_agent-originated error

The leaves carve that surface into three intents:

- :class:`ConfigError` -- you passed an invalid argument *value* or *type*, or
  the run is misconfigured (the bulk of the old ``raise ValueError`` /
  ``raise TypeError`` validation sites, plus the CLI's config parsing).
- :class:`StateError` -- a runtime invariant or lifecycle state was violated
  (e.g. resolving an already-resolved gate decision, an unrecognised gate
  disposition, a driver invariant). These previously leaked as a bare
  ``ValueError`` / ``RuntimeError``; the dedicated type lets callers tell a
  "you broke the protocol at runtime" error apart from a "you passed a bad
  argument" one.
- :class:`AsyncSeamInSyncLoop` -- an async (awaitable) seam was used inside the
  synchronous :func:`loop_agent.run_loop` (Issue #40). Relocated here so it
  shares the :class:`LoopError` base; still importable from
  :mod:`loop_agent._async` for backwards compatibility.
- :class:`SeamTimeout` -- an ``act`` / ``review`` / ``verify`` seam overran its per-call
  :class:`~loop_agent.loop.TimeoutPolicy` deadline under ``on_timeout="kill"``
  (Issue #42). A :class:`StateError` (a run-time invariant -- the seam did not
  finish in time). Relocated here for the unified hierarchy (Issue #71); still
  importable from :mod:`loop_agent.loop` for backwards compatibility.
- :class:`UnsupportedTimeoutKill` -- a hard-kill timeout was requested for a
  *synchronous* seam that cannot be interrupted on this platform/thread (Issue
  #42). A :class:`ConfigError` (the run's seam/platform combination is
  misconfigured for hard kill). Relocated here for the unified hierarchy (Issue
  #71); still importable from :mod:`loop_agent.loop` for backwards
  compatibility.

Backwards compatibility
=======================
Before this hierarchy, these sites raised the builtins ``ValueError`` /
``TypeError`` / ``RuntimeError`` directly, and both this project's tests and
external callers ``except`` those builtins. To avoid a breaking change, each
leaf *also* inherits the builtin(s) it used to raise (multiple inheritance):

- ``ConfigError`` is a ``ValueError`` **and** a ``TypeError``
- ``StateError`` is a ``ValueError`` **and** a ``RuntimeError``
- ``AsyncSeamInSyncLoop`` is a ``RuntimeError``
- ``SeamTimeout`` is a ``StateError`` (so transitively a ``ValueError`` and a
  ``RuntimeError``); it was a bare ``Exception`` before #71, so this only
  *widens* what catches it -- ``except SeamTimeout`` is unchanged.
- ``UnsupportedTimeoutKill`` is a ``ConfigError`` **and** a ``RuntimeError``; it
  used to be a bare ``RuntimeError``, so it explicitly keeps that base (via
  multiple inheritance) to preserve pre-#71 ``except RuntimeError`` callers
  while also joining the :class:`ConfigError` surface.

So an ``except ValueError`` / ``except TypeError`` / ``except RuntimeError``
written against the old API keeps working unchanged, while new code can catch
the precise :class:`LoopError` subtype (or :class:`LoopError` itself). The
builtin bases are a compatibility shim and may be dropped in a future major
version; prefer catching :class:`LoopError` or a specific subtype.

The one builtin deliberately kept *outside* this hierarchy is the ``KeyError``
that :func:`loop_agent.adapters.base.render_prompt` raises for a prompt template
referencing a missing context field -- it mirrors :meth:`str.format` /
``dict`` ``KeyError`` semantics on purpose, so callers can ``except KeyError``
it exactly as they would a missing-key lookup.
"""

from __future__ import annotations

__all__ = [
    "LoopError",
    "ConfigError",
    "StateError",
    "AsyncSeamInSyncLoop",
    "SeamTimeout",
    "UnsupportedTimeoutKill",
]


class LoopError(Exception):
    """Base class for every error raised by loop_agent.

    Catch this to handle any library-originated error in one place. Concrete
    errors are one of the subclasses below; the base is not raised directly.
    """


class ConfigError(LoopError, ValueError, TypeError):
    """An argument value is invalid, or the run is misconfigured.

    Raised by the library's *explicit* construction- and call-time validation:
    an invalid value (a stop condition built with a negative limit, a
    non-empty-string id left empty, an unknown enum value, ...), an explicit
    type/shape check (``conditions`` that is not an ``AnyOf``/sequence, a hook
    or resolver returning the wrong type, an unsupported adapter response), and
    the CLI's TOML/argument parsing. Inherits ``ValueError`` and ``TypeError``
    so pre-hierarchy ``except ValueError`` / ``except TypeError`` callers keep
    working.

    Note: this wraps the library's *own* validation. Passing an argument whose
    type violates the annotation to an un-checked numeric path (e.g.
    ``MaxIterations(None)``) still surfaces as a plain ``TypeError`` from the
    offending operation -- standard Python behaviour, not wrapped here.
    """


class StateError(LoopError, ValueError, RuntimeError):
    """A runtime invariant or lifecycle state was violated.

    Distinct from :class:`ConfigError` (bad *input*): this signals that an
    operation was attempted against a state that forbids it -- resolving a gate
    decision that is already resolved, executing/leasing a decision that is
    still pending or is non-executable, a proposed action that no longer matches
    the one a persisted decision was recorded for, an unrecognised gate
    disposition, or a driver invariant breaking. Inherits ``ValueError`` and
    ``RuntimeError`` so the builtins these sites used to raise still catch them.
    """


class AsyncSeamInSyncLoop(LoopError, RuntimeError):
    """An async (awaitable) seam was used inside the synchronous ``run_loop``.

    One of ``act`` / ``review`` / ``verify`` / ``gather`` / a condition's ``check`` /
    ``gate.review`` / ``on_step`` / ``on_complete`` returned an awaitable while
    the loop was driven synchronously. Use :func:`loop_agent.async_run_loop`
    for async seams (Issue #40). Inherits ``RuntimeError`` for backwards
    compatibility; canonical home is this module, re-exported from
    :mod:`loop_agent._async`.
    """


class SeamTimeout(StateError):
    """A loop seam exceeded its per-call timeout under ``on_timeout="kill"``.

    Raised *out of the loop* (so :func:`loop_agent.run_loop` /
    :func:`loop_agent.async_run_loop` does not return a ``LoopResult``) when
    ``act`` / ``review`` / ``verify`` overruns its configured
    :class:`~loop_agent.loop.TimeoutPolicy` deadline in hard-kill mode. For an
    async seam the underlying task has been cancelled (via :func:`asyncio.wait`
    + ``task.cancel()``); for a synchronous seam on a POSIX main thread it was
    interrupted by ``SIGALRM``. ``seam`` is ``"act"``, ``"review"``, or ``"verify"`` and
    ``seconds`` the deadline that was exceeded.

    A :class:`StateError` (a run-time invariant violation -- the seam did not
    finish within its allotted time), so it is also a :class:`LoopError` and,
    via :class:`StateError`'s compat bases, a ``RuntimeError`` and a
    ``ValueError``. Before Issue #71 it was a bare ``Exception``; that only
    widens what catches it, so ``except SeamTimeout`` callers are unaffected.
    Introduced in Issue #42 (canonical home moved here in Issue #71;
    re-exported from :mod:`loop_agent.loop`).
    """

    def __init__(self, seam: str, seconds: float) -> None:
        self.seam = seam
        self.seconds = seconds
        super().__init__(
            f"{seam!r} seam exceeded its {seconds:g}s per-call timeout (hard kill)"
        )


class UnsupportedTimeoutKill(ConfigError, RuntimeError):
    """A hard-kill timeout was requested for a *synchronous* seam that cannot be
    interrupted on this platform/thread.

    Hard-killing a blocking synchronous call requires POSIX ``SIGALRM`` on the
    main thread (:func:`signal.setitimer`). On Windows, or off the main thread,
    that mechanism is unavailable, so a synchronous seam cannot be *guaranteed*
    to be interrupted -- a genuinely hung call would never return. Rather than
    silently hang, the driver refuses up front: use an async seam (cancelled via
    the asyncio event loop, fully portable) or ``on_timeout="graceful"`` (which
    detects an overrun *after* the call returns; it cannot bound a hung call).

    A :class:`ConfigError` (the run's seam/platform combination is misconfigured
    for hard kill), so it is also a :class:`LoopError`, a ``ValueError`` and a
    ``TypeError``. It additionally keeps ``RuntimeError`` as an explicit base
    (multiple inheritance) because it *was* a bare ``RuntimeError`` before Issue
    #71 -- this preserves any pre-existing ``except RuntimeError`` callers while
    also joining the :class:`ConfigError` surface. Introduced in Issue #42
    (canonical home moved here in Issue #71; re-exported from
    :mod:`loop_agent.loop`).
    """
