# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Log retention: date-stamped files, allowlist-only deletion.

SAFETY NOTE, read before changing anything here.

`~/.agentstop/logs` is NOT this service's private directory. It is shared with
at least eight other producers -- LifeOS skill daemons, a pressure daemon, a
firewall-approval job -- and it contains:

  * `stdout.log` / `stderr.log`  launchd redirect targets for this very daemon,
                                 held open with a long-lived fd. Unlinking one
                                 does NOT error: launchd keeps writing into a
                                 deleted inode and all future output silently
                                 disappears until the service restarts.
  * `python-firewall-approve.log`  root-owned; unlink fails.
  * `daily-brief.jsonl`, `health-score.jsonl`, `weekly-review-*.jsonl`, ...
                                 other jobs' data, with their own value.
  * `routing.jsonl.backup-2026-07-30`  a hand-made backup.

Therefore deletion is driven by a STRICT ALLOWLIST of exact filenames this
service owns. Never `glob("*.jsonl")`, never `rmtree` a path built from
anything but a validated date. Anything not matching the allowlist is left
untouched, unconditionally.

Dates are UTC. Every `ts` in these files is `time.time()`, so a local-time
filename would drift out of alignment with the records inside -- precisely the
trap you would hit debugging an overnight incident.
"""

from __future__ import annotations

import datetime
import os
import pathlib
import re
import time

# Exact stems this service owns. Anything else in the directory is off-limits.
_STEMS = ("requests", "routing", "kills", "lifeos-escalations")
_LOG_RE = re.compile(
    r"^(?P<stem>" + "|".join(_STEMS) + r")"
    r"(?:-legacy)?-(?P<date>\d{4}-\d{2}-\d{2})\.jsonl\Z"
)
# Capture subdirectories are named by date; validated before anything inside
# them is touched.
# \Z not $: $ also matches before a trailing newline, so a file
# literally named "...jsonl\n" would sneak past an allowlist meant
# to be exact.
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")

_last_sweep = 0.0


def today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def log_path(log_dir: pathlib.Path, stem: str) -> pathlib.Path:
    """Current day's file for `stem`. Resolved per call so tests can repoint."""
    return log_dir / f"{stem}-{today_utc()}.jsonl"


def seal_legacy_logs(log_dir: pathlib.Path) -> list[str]:
    """Retire the pre-dating undated files, once.

    `routing.jsonl` spans months, so stamping it with any single date would be
    a lie, and its mtime says "today" for a file that is mostly ancient.
    Renaming it to `<stem>-legacy-<sealdate>.jsonl` is honest -- the date means
    "closed on" -- and lets the normal age policy expire it from there.

    Idempotent: only acts when an undated file is actually present.
    """
    sealed = []
    for stem in _STEMS:
        old = log_dir / f"{stem}.jsonl"
        if not old.exists():
            continue
        new = log_dir / f"{stem}-legacy-{today_utc()}.jsonl"
        if new.exists():          # already sealed today; leave both alone
            continue
        try:
            old.rename(new)
            sealed.append(new.name)
        except OSError:
            pass
    return sealed


def _expired(date_str: str, keep_days: int, today: str) -> bool:
    """True if `date_str` is older than the retention window.

    Never expires today's date regardless of `keep_days` -- an explicit guard
    rather than relying on the arithmetic, because this is the only thing
    standing between a misconfiguration and deleting a file being written to
    right now.
    """
    if date_str >= today:
        return False
    cutoff = (datetime.datetime.strptime(today, "%Y-%m-%d")
              - datetime.timedelta(days=keep_days)).strftime("%Y-%m-%d")
    return date_str < cutoff


def sweep_once(log_dir: pathlib.Path, metadata_days: int = 14,
               capture_days: int = 7) -> dict:
    """Delete expired Deflector-owned logs. Returns a summary for logging."""
    today = today_utc()
    # Clamp: a negative value would push the cutoff into the future and expire
    # everything, and a non-int from YAML would raise inside the sweep loop
    # where the blanket handler would silently retry forever.
    metadata_days = max(0, int(metadata_days))
    capture_days = max(0, int(capture_days))
    removed: list[str] = []
    kept_foreign = 0

    if not log_dir.exists():
        return {"removed": [], "foreign_untouched": 0}

    for entry in log_dir.iterdir():
        if entry.is_dir():
            continue
        m = _LOG_RE.match(entry.name)
        if not m:
            kept_foreign += 1          # not ours: never a deletion candidate
            continue
        if _expired(m.group("date"), metadata_days, today):
            try:
                entry.unlink()
                removed.append(entry.name)
            except OSError:
                pass                   # root-owned or vanished; skip quietly

    # Capture directories: validate the NAME as a date before touching contents.
    cap = log_dir / "capture"
    if cap.is_dir():
        for day_dir in cap.iterdir():
            if not day_dir.is_dir() or not _DAY_RE.match(day_dir.name):
                kept_foreign += 1
                continue
            if not _expired(day_dir.name, capture_days, today):
                continue
            for f in day_dir.iterdir():
                if f.suffix in (".cms", ".part"):
                    try:
                        f.unlink()
                        removed.append(f"capture/{day_dir.name}/{f.name}")
                    except OSError:
                        pass
            try:
                day_dir.rmdir()        # fails harmlessly if anything unexpected remains
            except OSError:
                pass

        # Orphaned .part files (crash mid-write) are ciphertext-only and
        # useless; reap them by age regardless of which day they sit under.
        cutoff = time.time() - 3600
        for day_dir in cap.iterdir():
            if not day_dir.is_dir() or not _DAY_RE.match(day_dir.name):
                continue
            for f in day_dir.glob("*.part"):
                try:
                    if f.stat().st_mtime < cutoff:
                        f.unlink()
                        removed.append(f"capture/{day_dir.name}/{f.name}")
                except OSError:
                    pass

    return {"removed": removed, "foreign_untouched": kept_foreign}
