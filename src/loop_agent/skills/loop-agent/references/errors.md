> This file is a load-on-demand bundled copy of `docs/errors.md`. The canonical source is `docs/errors.md` in the repository.

# Exception Hierarchy (LoopError)

All exceptions raised by loop-agent derive from the single base class `LoopError` (Issue #43).
This lets callers catch "errors originating from this library" in one place:

```python
from loop_agent import LoopError

try:
    run_loop(act=..., verify=..., conditions=...)
except LoopError as exc:
    # Handle any error originating from loop_agent here.
    ...
```

## Hierarchy

```
LoopError(Exception)                                     Base for all library errors
├── ConfigError(LoopError, ValueError, TypeError)        Invalid argument value/type or configuration error
│   └── UnsupportedTimeoutKill(ConfigError, RuntimeError) Environment/thread where synchronous seam hard-kill is unavailable (#42)
├── StateError(LoopError, ValueError, RuntimeError)      Runtime invariant/lifecycle violation
│   └── SeamTimeout(StateError)                          Per-call timeout kill triggered (#42)
└── AsyncSeamInSyncLoop(LoopError, RuntimeError)         Async seam passed to synchronous run_loop (#40)
```

The canonical definitions live in `loop_agent.errors`. The same classes can also be referenced from the `loop_agent` top level (`from loop_agent import LoopError, ConfigError, StateError, AsyncSeamInSyncLoop, SeamTimeout, UnsupportedTimeoutKill`) and, for backward compatibility, from `loop_agent.cli.ConfigError` / `loop_agent._async.AsyncSeamInSyncLoop` / `loop_agent.loop.SeamTimeout` / `loop_agent.loop.UnsupportedTimeoutKill`.

> `SeamTimeout` / `UnsupportedTimeoutKill` were introduced in #42 (per-call timeout/kill) and originally lived outside the `LoopError` hierarchy (as plain `Exception` / `RuntimeError`, respectively). #71 integrated them into the hierarchy as shown above (behavior and attributes are unchanged).

### Meaning of Each Type

| Type | When it is raised | Example |
|----|------------------|-----|
| `ConfigError` | An argument **value** that the library **explicitly validates** is invalid, an explicit **type/shape check** fails, or the run is misconfigured (construction-time / call-time validation). Also includes CLI TOML / argument parsing configuration errors | `MaxIterations(-1)`, empty string id, `conditions` is not `AnyOf`/sequence, invalid hook/resolver return type, unknown enum value, missing `[act]` table |
| `StateError` | Runtime **invariant / state** violation. This is not "invalid input" but "an operation that is not allowed in the current state" | Re-resolving an already resolved gate decision, execute/lease of an unresolved or unexecutable decision, proposed action on resume does not match the record, unknown gate disposition, defensive driver invariant |
| `AsyncSeamInSyncLoop` | An awaitable seam (`act`/`review`/`verify`/`gather`/`condition.check`/`gate.review`/`on_step`/`on_complete`) was passed to synchronous `run_loop` | Use `await async_run_loop(...)` for async hooks (#40) |
| `SeamTimeout` (derived from `StateError`) | `act`/`review`/`verify` exceeded an `on_timeout="kill"` per-call deadline, and that seam was cancelled/interrupted (#42). "Did not complete within the specified time" is treated as a runtime invariant violation under `StateError` | `act` times out under `TimeoutPolicy(act=…, on_timeout="kill")` -> `except SeamTimeout as e: e.seam, e.seconds` |
| `UnsupportedTimeoutKill` (derived from `ConfigError` + `RuntimeError`) | A hard-kill was requested for a **synchronous** seam, but interruption cannot be guaranteed because POSIX main-thread `SIGALRM` is unavailable (Windows / non-main thread) (#42). This is under `ConfigError` as a configuration mismatch between the seam and environment. `RuntimeError` is also retained as a base for compatibility with pre-#71 `except RuntimeError` | Synchronous seam + `on_timeout="kill"` in a non-POSIX environment. Use an async seam or `on_timeout="graceful"` |

> `ConfigError` covers validation performed by the library **itself**. If a value that violates type hints is passed into an unchecked numeric path (for example, `MaxIterations(None)`), that operation raises a plain `TypeError` (standard Python behavior, not wrapped here).

## Backward Compatibility (multiple inheritance)

Before this hierarchy was introduced, these code paths directly raised the built-in exceptions `ValueError` / `TypeError` / `RuntimeError`, and this project's tests and external callers caught those exceptions with `except`. **To avoid a breaking change**, each leaf also inherits from the built-in exception it used to raise:

- `ConfigError` is both `ValueError` and `TypeError`
- `StateError` is both `ValueError` and `RuntimeError`
- `AsyncSeamInSyncLoop` is `RuntimeError`
- `SeamTimeout` derives from `StateError` (and is therefore also `ValueError` / `RuntimeError`). Before #71 it was a plain `Exception`, so this only **broadens** what catches it; `except SeamTimeout` is unchanged
- `UnsupportedTimeoutKill` derives from `ConfigError` (`ValueError` / `TypeError`) and is also **explicitly `RuntimeError`**. Before #71 it was a plain `RuntimeError`, so `RuntimeError` remains a base to avoid breaking existing `except RuntimeError` handlers

Therefore, `except ValueError` / `except TypeError` / `except RuntimeError` handlers written against the old API continue to work, while new code can catch precise `LoopError` subtypes (or `LoopError` itself):

```python
from loop_agent import ConfigError, StateError

try:
    MaxIterations(-1)
except ConfigError:   # Precise: configuration error
    ...
except ValueError:    # Legacy: still catches this
    ...
```

> The built-in exception bases are compatibility shims and may be removed in a future major version.
> New code should catch `LoopError` or a concrete subtype.

## Error Chaining

Code paths that translate built-in exceptions preserve the cause with `raise ... from exc`. For example, `loop_agent.transport` translates a `TypeError` from a non-JSON-serializable `Wake.payload` into `ConfigError` while preserving the original exception in `__cause__`:

```python
try:
    transport_enqueue(wake_with_unserializable_payload)
except ConfigError as exc:
    assert isinstance(exc.__cause__, TypeError)  # The cause can be inspected.
```

## One Exception Outside the Hierarchy: prompt template KeyError

`loop_agent.adapters.base.render_prompt` **intentionally raises `KeyError`** when a prompt template references a field that is not present in the context (to preserve the `KeyError` semantics of `str.format` / `dict` and allow missing keys to be handled with `except KeyError`). This is the **only built-in exception** that does not belong to the `LoopError` hierarchy, and it is an intentional design choice.
