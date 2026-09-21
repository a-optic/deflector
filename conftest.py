# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Global pytest fixtures.

Exists because tests were writing into the OPERATOR'S REAL LOG DIRECTORY.
`main._log_kill` and friends resolve `LOG_DIR` at call time from a module
global, so any test that exercises a code path reaching a writer -- without
patching that global -- appends to `~/.agentstop/logs/`. `test_supervised_stream.py`
drives `_supervised_stream` with a fake clock and never patched it, so 53 of
the 74 records in the live `kills.jsonl` were pytest artifacts (`"ts": 100.0`,
`"id": "req-1"`) sitting in production telemetry.

Two fixtures, one prevention and one detection. The detection half matters:
an autouse redirect silently fixes the problem AND silently hides the next
writer that hardcodes a path or writes from a subprocess, where monkeypatch
cannot reach.
"""

import os
import pathlib
import re

import pytest

import main


@pytest.fixture(autouse=True)
def isolate_log_dir(tmp_path, monkeypatch):
    """Point every log writer at a per-test tmp dir.

    Autouse: opt-out would mean remembering, and forgetting is exactly how the
    kills.jsonl pollution happened. Works for all writers because each resolves
    `LOG_DIR` at call time rather than capturing it at import.

    Tests that patch LOG_DIR themselves (test_lifeos_gate.py does) still work --
    monkeypatch unwinds LIFO, so the inner patch simply wins.
    """
    log_dir = tmp_path / "logs"
    (log_dir / "capture").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(main, "LOG_DIR", log_dir)
    return log_dir


@pytest.fixture(scope="session", autouse=True)
def real_log_dir_untouched():
    """Fail the run if anything wrote to the operator's real log directory.

    The autouse redirect above handles everything that goes through
    `main.LOG_DIR`. This catches what it cannot: a hardcoded path, a writer
    added to a module that does not consult that global, or a subprocess.
    Compares name -> size for every entry, so both new files and growth of
    existing ones are caught.

    Snapshot is taken inside the fixture rather than at import so it runs after
    `main` is imported (importing main mkdir's the real log dir, which is
    harmless -- it already exists -- but would otherwise race the snapshot).
    """
    real = pathlib.Path(os.path.expanduser(main.CFG["logging"]["log_dir"]))

    def snapshot() -> dict[str, int]:
        if not real.exists():
            return {}
        out = {}
        for p in real.iterdir():
            try:
                out[p.name] = p.stat().st_size
            except OSError:
                pass  # permission-denied (root-owned) entries: presence is enough
        return out

    before = snapshot()
    yield
    after = snapshot()

    # The daemon may be running and serving real traffic while the suite runs,
    # continuously appending to its launchd stdout/stderr and to its own dated
    # logs. Failing on that would make the suite flake whenever a single client
    # request overlaps a test run. So: growth of files this service legitimately
    # writes at runtime is ignored, while any NEW file, or growth of anything
    # else, still fails -- which is what actually detects a test leaking.
    owned_runtime = ("stdout.log", "stderr.log", "python-firewall-approve.log")

    def is_runtime_noise(name: str) -> bool:
        return name in owned_runtime or bool(
            re.match(r"^(requests|routing|kills|lifeos-escalations)"
                     r"(?:-legacy)?-\d{4}-\d{2}-\d{2}\.jsonl$", name)
        )

    added = {k for k in set(after) - set(before) if not is_runtime_noise(k)}
    grew = {k for k in set(before) & set(after)
            if before[k] != after[k] and not is_runtime_noise(k)}
    if added or grew:
        pytest.fail(
            "tests wrote to the REAL log dir "
            f"({real}): added={sorted(added)} grew={sorted(grew)}"
        )


@pytest.fixture(autouse=True)
def reset_capture_globals():
    """Restore capture.py's module globals between tests.

    Several tests deliberately poke `capture._openssl` / `capture._queue` to
    exercise failure paths, and a leftover `_queue` stays bound to a closed
    event loop. Harmless today only because each test reconfigures first --
    which is exactly the kind of implicit ordering dependency that breaks the
    next test someone adds.
    """
    import capture
    saved = (capture._openssl, capture._queue, capture._cfg,
             capture._recipients, capture._log)
    yield
    (capture._openssl, capture._queue, capture._cfg,
     capture._recipients, capture._log) = saved


@pytest.fixture(autouse=True)
def disable_retrieval(monkeypatch):
    """Never let the suite perform live cross-session retrieval.

    Exists because enabling `retrieval` in config.yaml immediately broke the
    suite in two ways, and only one of them was a failing assertion.

    The visible one: retrieval inserts its system message at messages[0], so a
    test reading messages[0] to find the user turn silently read the injected
    block instead.

    The one that mattered more: every test driving the app through TestClient
    started making real HTTP calls to Open Brain on 127.0.0.1:8000 and pulling
    the operator's actual memories into request bodies -- which then appeared
    verbatim in pytest failure output. That is a non-hermetic suite AND
    personal content in test logs.

    Autouse for the same reason isolate_log_dir is: opting out would mean
    remembering, and forgetting is how it happened the first time. Tests that
    exercise retrieval set their own `retrieval` block, which lands after this
    and therefore wins.
    """
    monkeypatch.setitem(main.CFG, "retrieval", {"enabled": False})
