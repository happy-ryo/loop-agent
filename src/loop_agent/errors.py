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

Backwards compatibility
=======================
Before this hierarchy, these sites raised the builtins ``ValueError`` /
``TypeError`` / ``RuntimeError`` directly, and both this project's tests and
external callers ``except`` those builtins. To avoid a breaking change, each
leaf *also* inherits the builtin(s) it used to raise (multiple inheritance):

- ``ConfigError`` is a ``ValueError`` **and** a ``TypeError``
- ``StateError`` is a ``ValueError`` **and** a ``RuntimeError``
- ``AsyncSeamInSyncLoop`` is a ``RuntimeError``

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
]


class LoopError(Exception):
    """Base class for every error raised by loop_agent.

    Catch this to handle any library-originated error in one place. Concrete
    errors are one of the subclasses below; the base is not raised directly.
    """


class ConfigError(LoopError, ValueError, TypeError):
    """An argument value/type is invalid, or the run is misconfigured.

    Raised by construction- and call-time validation (a stop condition built
    with a negative limit, a non-empty-string id left empty, a hook given the
    wrong type, an unknown enum value, ...) and by the CLI's TOML/argument
    parsing. Inherits ``ValueError`` and ``TypeError`` so pre-hierarchy
    ``except ValueError`` / ``except TypeError`` callers keep working.
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

    One of ``act`` / ``verify`` / ``gather`` / a condition's ``check`` /
    ``gate.review`` / ``on_step`` / ``on_complete`` returned an awaitable while
    the loop was driven synchronously. Use :func:`loop_agent.async_run_loop`
    for async seams (Issue #40). Inherits ``RuntimeError`` for backwards
    compatibility; canonical home is this module, re-exported from
    :mod:`loop_agent._async`.
    """
