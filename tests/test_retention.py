# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Retention safety tests.

`~/.agentstop/logs` is shared with ~8 other producers. The tests that matter
most here are the ones proving the sweep does NOT delete files it does not
own -- particularly `stdout.log`, which launchd holds open, and whose deletion
would fail SILENTLY (the daemon keeps writing into a dead inode) rather than
erroring.
Run: .venv/bin/python -m pytest tests/test_retention.py -q
"""

import datetime

import pytest

import retention


def _d(days_ago: int) -> str:
    return (datetime.datetime.now(datetime.timezone.utc)
            - datetime.timedelta(days=days_ago)).strftime("%Y-%m-%d")


@pytest.fixture
def logs(tmp_path):
    # exist_ok: conftest's autouse isolate_log_dir fixture has already created
    # this same path for the test, which is exactly the isolation working.
    d = tmp_path / "logs"
    (d / "capture").mkdir(parents=True, exist_ok=True)
    return d


def test_foreign_files_are_never_deleted(logs):
    """The single most important test in this file.

    Every one of these belongs to another producer. `stdout.log` in particular
    is launchd-held-open and owned by the daemon's user, so a permissive glob
    would delete
    it SUCCESSFULLY and silently break all future daemon output.
    """
    foreign = [
        "stdout.log", "stderr.log", "python-firewall-approve.log",
        "daily-brief.jsonl", "health-score.jsonl", "presence-check.jsonl",
        "weekly-review-security.jsonl", "pressure.log",
        "routing.jsonl.backup-2026-07-30",
        "escalation-audit.jsonl", "homelab-drift-log.jsonl",
    ]
    for name in foreign:
        p = logs / name
        p.write_text("not ours\n")
        # ancient, to prove age alone never justifies deletion
        import os
        os.utime(p, (0, 0))

    expired = logs / f"routing-{_d(400)}.jsonl"
    expired.write_text("ours, ancient\n")

    res = retention.sweep_once(logs, metadata_days=14, capture_days=7)

    for name in foreign:
        assert (logs / name).exists(), f"deleted a foreign file: {name}"
    assert not expired.exists(), "failed to delete an expired owned file"
    assert res["removed"] == [expired.name]


def test_todays_file_survives_even_with_zero_retention(logs):
    """The live file must never be a deletion candidate."""
    today = logs / f"requests-{_d(0)}.jsonl"
    today.write_text("live\n")
    retention.sweep_once(logs, metadata_days=0, capture_days=0)
    assert today.exists()


def test_metadata_and_capture_have_independent_windows(logs):
    meta_old = logs / f"requests-{_d(10)}.jsonl"      # inside 14d
    meta_dead = logs / f"requests-{_d(20)}.jsonl"     # outside 14d
    meta_old.write_text("x"); meta_dead.write_text("x")

    cap_old = logs / "capture" / _d(5)                # inside 7d
    cap_dead = logs / "capture" / _d(10)              # outside 7d
    cap_old.mkdir(); cap_dead.mkdir()
    (cap_old / "a.cms").write_bytes(b"ct")
    (cap_dead / "b.cms").write_bytes(b"ct")

    retention.sweep_once(logs, metadata_days=14, capture_days=7)

    assert meta_old.exists() and not meta_dead.exists()
    assert (cap_old / "a.cms").exists()
    assert not (cap_dead / "b.cms").exists()


def test_capture_dir_with_non_date_name_is_untouched(logs):
    """Guard against rmtree-ing anything whose name is not a validated date."""
    weird = logs / "capture" / "important-do-not-delete"
    weird.mkdir()
    (weird / "keep.cms").write_bytes(b"x")
    retention.sweep_once(logs, metadata_days=0, capture_days=0)
    assert (weird / "keep.cms").exists()


def test_orphaned_part_files_are_reaped(logs):
    """A crash mid-write leaves ciphertext-only `.part`; reap by age."""
    import os
    day = logs / "capture" / _d(0)
    day.mkdir()
    stale = day / "abc.cms.part"
    fresh = day / "def.cms.part"
    stale.write_bytes(b"partial"); fresh.write_bytes(b"partial")
    os.utime(stale, (0, 0))                      # 1970 -> older than 1h
    retention.sweep_once(logs, metadata_days=14, capture_days=7)
    assert not stale.exists()
    assert fresh.exists()


def test_sealing_is_idempotent_and_renames_undated(logs):
    (logs / "routing.jsonl").write_text("old\n")
    (logs / "kills.jsonl").write_text("old\n")

    first = retention.seal_legacy_logs(logs)
    assert len(first) == 2
    assert not (logs / "routing.jsonl").exists()
    assert (logs / f"routing-legacy-{_d(0)}.jsonl").exists()

    second = retention.seal_legacy_logs(logs)     # nothing left to seal
    assert second == []


def test_sealed_files_are_swept_by_normal_policy(logs):
    old = logs / f"routing-legacy-{_d(30)}.jsonl"
    old.write_text("sealed long ago\n")
    retention.sweep_once(logs, metadata_days=14, capture_days=7)
    assert not old.exists()


def test_sweep_on_missing_dir_is_safe(tmp_path):
    res = retention.sweep_once(tmp_path / "nope")
    assert res["removed"] == []


def test_expired_boundary_is_exclusive(logs):
    """A file exactly at the retention edge is kept, not deleted."""
    edge = logs / f"kills-{_d(14)}.jsonl"
    past = logs / f"kills-{_d(15)}.jsonl"
    edge.write_text("x"); past.write_text("x")
    retention.sweep_once(logs, metadata_days=14, capture_days=7)
    assert edge.exists()
    assert not past.exists()
