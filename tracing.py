# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Per-request trace context.

Exists so a single request can be followed across all four log files. Before
this, `routing.jsonl`, `kills.jsonl`, `lifeos-escalations.jsonl` and
`requests.jsonl` shared no identifier, and correlating them meant eyeballing
timestamps by hand -- which is exactly what made the last outage take hours.

The context is a MUTABLE DICT held in a ContextVar, rather than one ContextVar
per field. That is deliberate: `_log_kill` runs deep inside an async generator
(`_supervised_stream`), while the code that needs to read the kill reason runs
in the wrapping generator's `finally`. Whether a value *set* inside an async
generator propagates back out depends on CPython's asyncgen context semantics,
which have shifted across versions. Mutating a dict everyone already holds a
reference to sidesteps that question entirely.
"""

from __future__ import annotations

import contextvars
import secrets
import time

# None outside a request (e.g. startup tasks, tests calling writers directly).
TRACE_CTX: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "deflector_trace_ctx", default=None
)


def new_trace_id() -> str:
    """Sortable-ish, collision-resistant id.

    Uses `secrets.token_hex` rather than `id(request)` -- CPython reuses object
    addresses, so `id()` collides across requests and makes a poor key for
    joining logs or naming capture files.
    """
    return f"{int(time.time() * 1000)}-{secrets.token_hex(8)}"


def start(trace_id: str, **fields) -> dict:
    """Install a fresh context for this request and return it."""
    ctx: dict = {"id": trace_id, "t0": time.time(), **fields}
    TRACE_CTX.set(ctx)
    return ctx


def get() -> dict | None:
    return TRACE_CTX.get()


def trace_id() -> str | None:
    ctx = TRACE_CTX.get()
    return ctx["id"] if ctx else None


def note(**fields) -> None:
    """Record something on the current request's context, if there is one.

    Safe to call from anywhere: a no-op outside a request rather than an error,
    so log writers can call it unconditionally.
    """
    ctx = TRACE_CTX.get()
    if ctx is not None:
        ctx.update(fields)
