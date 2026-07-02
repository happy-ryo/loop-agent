# Exception Hierarchy (`LoopError`)

All exceptions raised by loop-agent derive from the single base class `LoopError` (Issue #43).
This makes it possible to catch "errors from this library" in one place:

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
LoopError(Exception)                                  Base for all library errors
├── ConfigError(LoopError, ValueError, TypeError)        Invalid argument value/type or configuration error
│   └── UnsupportedTimeoutKill(ConfigError, RuntimeError) Environment/thread where sync seam hard-kill is unavailable (#42)
├── StateError(LoopError, ValueError, RuntimeError)      Runtime invariant/lifecycle violation
│   └── SeamTimeout(StateError)                          per-call timeout kill fired (#42)
└── AsyncSeamInSyncLoop(LoopError, RuntimeError)         Async seam passed to sync run_loop (#40)
```

The canonical definitions live in `loop_agent.errors`. The same classes are also available from the `loop_agent` top level (`from loop_agent import LoopError, ConfigError, StateError, AsyncSeamInSyncLoop, SeamTimeout, UnsupportedTimeoutKill`) and, for backward compatibility, from `loop_agent.cli.ConfigError` / `loop_agent._async.AsyncSeamInSyncLoop` / `loop_agent.loop.SeamTimeout` / `loop_agent.loop.UnsupportedTimeoutKill`.

> `SeamTimeout` / `UnsupportedTimeoutKill` were introduced in #42 (per-call timeout/kill) and originally lived outside the `LoopError` hierarchy (as plain `Exception` / `RuntimeError`, respectively). #71 integrated them into the hierarchy shown above, without changing behavior or attributes.

### Meaning of Each Type

| Type | When it is raised | Example |
|----|------------------|-----|
| `ConfigError` | An argument **value** that the library **explicitly validates** is invalid, an argument violates an explicit **type/shape check**, or the run is misconfigured (validation during construction / invocation). Also includes CLI TOML / argument parsing configuration errors | `MaxIterations(-1)`, empty string id, `conditions` is not `AnyOf`/a sequence, invalid return type from a hook/resolver, unknown enum value, missing `[act]` table |
| `StateError` | Runtime **invariant / state** violation. This is not "invalid input", but "an operation that is not allowed in the current state" | Resolving an already resolved gate decision again, executing/leasing an unresolved or non-executable decision, proposed action during resume does not match the recorded action, unknown gate disposition, defensive driver invariant |
| `AsyncSeamInSyncLoop` | An awaitable seam (`act`/`review`/`verify`/`gather`/`condition.check`/`gate.review`/`on_step`/`on_complete`) is passed to synchronous `run_loop` | Use `await async_run_loop(...)` for async hooks (#40) |
| `SeamTimeout` (`StateError` subclass) | `act`/`review`/`verify` exceeded a per-call deadline with `on_timeout="kill"` and that seam was canceled/interrupted (#42). "Did not complete within the allotted time" is treated as a runtime invariant violation under `StateError` | `act` times out with `TimeoutPolicy(act=..., on_timeout="kill")` -> `except SeamTimeout as e: e.seam, e.seconds` |
| `UnsupportedTimeoutKill` (`ConfigError` subclass + `RuntimeError`) | A hard-kill was requested for a **synchronous** seam, but interruption cannot be guaranteed because POSIX main-thread `SIGALRM` is unavailable (Windows / non-main thread) (#42). This belongs under `ConfigError` as a configuration mismatch between the seam and environment. It also keeps `RuntimeError` as a base for compatibility with pre-#71 `except RuntimeError` code | Sync seam + `on_timeout="kill"` in a non-POSIX environment. Use an async seam or `on_timeout="graceful"` |

> `ConfigError` wraps validations performed by the library **itself**. If a value that violates type hints is passed into an unchecked numeric path (for example, `MaxIterations(None)`), that operation raises the plain `TypeError` from Python's standard behavior; it is not wrapped here.

## Backward Compatibility (Multiple Inheritance)

Before this hierarchy was introduced, these locations raised built-in exceptions `ValueError` / `TypeError` /
`RuntimeError` directly, and both this project's tests and external callers caught those exceptions with
`except`. **To avoid a breaking change**, each leaf also inherits from the built-in exception types it used to raise:

- `ConfigError` is both `ValueError` and `TypeError`
- `StateError` is both `ValueError` and `RuntimeError`
- `AsyncSeamInSyncLoop` is `RuntimeError`
- `SeamTimeout` derives from `StateError` (and is therefore also `ValueError` / `RuntimeError`). Before #71 it was a plain `Exception`, so this only **widens** the catch surface; `except SeamTimeout` is unchanged
- `UnsupportedTimeoutKill` derives from `ConfigError` (`ValueError` / `TypeError`) and also **explicitly from `RuntimeError`**. Before #71 it was a plain `RuntimeError`, so `RuntimeError` remains a base to avoid breaking existing `except RuntimeError` handlers

Therefore, `except ValueError` / `except TypeError` /
`except RuntimeError` written against the old API continues to work, while new code can catch precise
`LoopError` subtypes (or `LoopError` itself):

```python
from loop_agent import ConfigError, StateError

try:
    MaxIterations(-1)
except ConfigError:   # Precise: configuration error
    ...
except ValueError:    # Legacy: this still catches it
    ...
```

> The built-in exception bases are compatibility shims and may be removed in a future major version.
> New code should catch `LoopError` or a concrete subtype.

## Error Chaining

Locations that translate built-in exceptions preserve the cause with `raise ... from exc`. For example,
`loop_agent.transport` translates the `TypeError` from a non-JSON-serializable `Wake.payload` into
`ConfigError`, while preserving the original exception in `__cause__`:

```python
try:
    transport_enqueue(wake_with_unserializable_payload)
except ConfigError as exc:
    assert isinstance(exc.__cause__, TypeError)  # The cause can be inspected.
```

## One Exception Outside the Hierarchy: Prompt Template `KeyError`

`loop_agent.adapters.base.render_prompt` intentionally raises **`KeyError`** when a prompt template references a field that is absent from the context. This preserves the `KeyError` semantics of `str.format` / `dict`, allowing missing keys to be handled with `except KeyError`. This is the **only built-in exception** intentionally outside the `LoopError` hierarchy, by design.
