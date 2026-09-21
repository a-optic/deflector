# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""
Cloud client timeout tests: connect/read must fail fast on a dead upstream,
without shortening the ceiling for main/tasks or the overall cloud budget.
Run: .venv/bin/python -m pytest tests/test_cloud_timeout.py -q
"""

import main


def test_cloud_client_has_fast_connect_and_read_bounds():
    t = main._clients["cloud"].timeout
    assert t.connect == main.CFG["upstream"]["cloud_connect_timeout_s"]
    assert t.read == main.CFG["upstream"]["cloud_read_timeout_s"]
    assert t.connect < 30
    assert t.read < 120


def test_cloud_client_keeps_overall_ceiling_for_write_and_pool():
    t = main._clients["cloud"].timeout
    assert t.write == main.CFG["upstream"]["cloud_timeout_s"]
    assert t.pool == main.CFG["upstream"]["cloud_timeout_s"]


def test_main_and_tasks_keep_the_overall_ceiling_but_bound_reads():
    # These used the flat 900s ceiling for reads too, which meant a locally
    # hung stream sat there for the full 15 minutes: the supervisor's idle-gap
    # stall check runs inside the chunk loop, so it never even wakes up for a
    # stream that has gone completely silent. The read bound is what actually
    # ends that case.
    for key in ("main", "tasks"):
        t = main._clients[key].timeout
        assert t.connect == main.CFG["upstream"]["timeout_s"]
        assert t.read == main.CFG["upstream"]["local_read_timeout_s"]
        assert t.read < main.CFG["upstream"]["timeout_s"]


def test_timeout_budgets_are_ordered_innermost_first():
    # The invariant that actually matters, and the one that was broken: httpx
    # applies local_read_timeout_s as the maximum gap BETWEEN READS, so if it
    # sits below the supervisor's idle budget it aborts a legitimately
    # buffering tool call before the supervisor ever judges it -- the read
    # timeout silently becomes the real stall policy. At 180s vs a 240s tool
    # budget it already did.
    read = main.CFG["upstream"]["local_read_timeout_s"]
    assert main.TH["stall_idle_s"] < main.TH["stall_idle_tools_s"] < read
    assert read < main.TH["max_wall_seconds"]


def test_tool_budget_stays_under_the_client_idle_timeout():
    # Pi's httpIdleTimeoutMs is 300s. Killing below that means a genuinely dead
    # request comes back as a clean finish_reason the client can report, rather
    # than the client giving up first on a bare socket timeout.
    assert main.TH["stall_idle_tools_s"] < 300


def test_local_read_timeout_leaves_room_for_prefill():
    # This bound also covers time-to-first-byte. A large agent payload can sit
    # in prefill for a long stretch before emitting anything, and aborting
    # those healthy requests would be a worse failure than a slow freeze.
    assert main.CFG["upstream"]["local_read_timeout_s"] >= 120


def test_silent_cooldown_outlasts_the_read_timeout_that_detects_it():
    # The ordering that makes the cooldown worth having. It is recorded only
    # after a full cloud_read_timeout_s of silence, so a cooldown shorter than
    # that timeout would lapse before the next turn could benefit and the
    # client would pay the stall again on every turn -- which is the failure
    # being fixed, not a smaller version of it.
    cd = main.CFG["upstream"]["cloud_silent_cooldown_s"]
    assert cd > main.CFG["upstream"]["cloud_read_timeout_s"]
    # Short enough that a recovered provider is picked up promptly. A tier
    # being unreachable is a claim with a much shorter shelf life than the
    # 24h account-refusal cooldowns.
    assert cd <= 3600
