# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Hide models from the thin-client dropdown after the upstream refuses them.

Offering a model that cannot answer is worse than not offering it: Pi surfaces
the failure as a generic connection error, and the operator re-picks the same
dead entry because nothing in the UI says otherwise.

The refusals are NOT equivalent, which is the whole reason this is keyed by
status code rather than a single timer. Observed from ollama.com, verbatim:

    402  "this model requires a subscription or extra usage, upgrade for
          access at https://ollama.com/upgrade"
    429  "you (<account>) have reached your session usage limit"
    410  "qwen3-coder:480b was retired at 2026-07-15 00:00:00 -0700 PDT"

402 and 429 are account state and clear on their own, so they earn a timed
cooldown. 410 is retirement -- the model is gone and is never coming back, so a
timed cooldown would return a permanently-dead entry to the dropdown every day
forever. Those get suppressed until an operator clears them.

A fourth case has no status at all: on 2026-09-08 ollama.com accepted
connections and returned zero bytes for every model. `record_unavailable`
covers that one, storing status "silent" so the two origins stay
distinguishable in the state file. `is_suppressed` reads only the expiry, so
every consumer treats both alike without needing to know which happened.

State lives in ~/.agentstop/model-cooldowns.json so a restart does not
resurrect a dead model. Local-only, never transmitted; contains model ids and
upstream error text, no prompt content.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time

STATE_PATH = pathlib.Path(os.path.expanduser("~/.agentstop/model-cooldowns.json"))

# Sentinel for "no expiry" -- stored as JSON null.
PERMANENT = None

_cache: dict | None = None
_mtime: float | None = None


def _load() -> dict:
    global _cache, _mtime
    try:
        mt = STATE_PATH.stat().st_mtime
    except OSError:
        _cache, _mtime = {}, None
        return {}
    if _cache is None or mt != _mtime:
        try:
            doc = json.loads(STATE_PATH.read_text())
            _cache = doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            # A corrupt state file must not take the proxy down, and must not
            # silently suppress every model either -- start from empty.
            _cache = {}
        _mtime = mt
    return _cache


def _save(state: dict) -> None:
    global _cache, _mtime
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(STATE_PATH.parent), suffix=".part")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, STATE_PATH)          # atomic; no torn read by a reader
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _cache = state
    try:
        _mtime = STATE_PATH.stat().st_mtime
    except OSError:
        _mtime = None


def _durations(cfg: dict) -> dict[int, float | None]:
    """status -> seconds, or PERMANENT. YAML may key these as int or str."""
    out: dict[int, float | None] = {}
    for k, v in (cfg or {}).items():
        try:
            status = int(k)
        except (TypeError, ValueError):
            continue
        if isinstance(v, str) and v.strip().lower() == "permanent":
            out[status] = PERMANENT
        else:
            try:
                out[status] = max(0.0, float(v))
            except (TypeError, ValueError):
                continue
    return out


def record(model: str, status: int, detail: str, cfg: dict) -> bool:
    """Suppress `model` if `status` is a configured refusal. True if recorded.

    Deliberately keyed on the UPSTREAM model id, which is what the dropdown
    lists. Routing aliases (lifeos-cloud-*) are intentionally NOT suppressed
    when their cloud target is: those lanes fall back to a local model, so they
    still work when the cloud side is refusing.
    """
    durations = _durations(cfg)
    if status not in durations or not model:
        return False
    secs = durations[status]
    state = dict(_load())
    state[model] = {
        "status": status,
        "since": time.time(),
        "until": None if secs is PERMANENT else time.time() + secs,
        "detail": (detail or "")[:300],
    }
    _save(state)
    return True


def record_unavailable(model: str, secs: float, detail: str) -> bool:
    """Suppress `model` for `secs` after a failure that carried no status.

    `record` looks a status up in the configured refusal table; a tier that
    accepts the connection and then answers with nothing never produces one.
    Rather than teach that function a second vocabulary -- or invent a fake
    status, which would put a number that never came off the wire into both
    this file and the logs -- silence gets its own entry point.

    Same state shape and same file, so `is_suppressed` covers both without
    knowing the difference. `status` is the string "silent" so an operator
    reading model-cooldowns.json can tell the two origins apart.

    Always timed, never PERMANENT: a tier being unreachable is a claim with a
    short shelf life, unlike a 410 retirement.
    """
    if not model or secs <= 0:
        return False
    state = dict(_load())
    state[model] = {
        "status": "silent",
        "since": time.time(),
        "until": time.time() + secs,
        "detail": (detail or "")[:300],
    }
    _save(state)
    return True


def is_suppressed(model: str, now: float | None = None) -> bool:
    entry = _load().get(model)
    if not entry:
        return False
    until = entry.get("until")
    if until is None:                       # permanent (e.g. retired)
        return True
    return (now or time.time()) < until


def active(now: float | None = None) -> dict:
    """Currently-suppressed models. Expired entries are omitted, not deleted --
    pruning is a write, and reads happen on the request path."""
    return {m: e for m, e in _load().items() if is_suppressed(m, now)}


def clear(model: str | None = None) -> int:
    """Un-suppress one model, or all. Returns how many entries were removed."""
    state = dict(_load())
    if model is None:
        n = len(state)
        _save({})
        return n
    if model in state:
        del state[model]
        _save(state)
        return 1
    return 0
