#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Show or clear models suppressed from the thin-client dropdown.

  deflector-cooldowns.py                  what is hidden right now, and why
  deflector-cooldowns.py --all            include entries whose cooldown expired
  deflector-cooldowns.py --clear <model>  put one model back in the dropdown
  deflector-cooldowns.py --clear-all      put everything back

A 410 (model retired upstream) never expires on its own -- clearing it by hand
is the only way back, which is deliberate: it is not coming back.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import model_cooldown  # noqa: E402


def _fmt(ts: float | None) -> str:
    if ts is None:
        return "never (retired)"
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--all", action="store_true",
                    help="include entries whose cooldown has already expired")
    ap.add_argument("--clear", metavar="MODEL")
    ap.add_argument("--clear-all", action="store_true")
    args = ap.parse_args()

    if args.clear_all:
        print(f"cleared {model_cooldown.clear()} entr(ies)")
        return 0
    if args.clear:
        n = model_cooldown.clear(args.clear)
        print(f"cleared {args.clear}" if n else f"no cooldown for {args.clear}")
        return 0 if n else 1

    entries = model_cooldown._load() if args.all else model_cooldown.active()
    if not entries:
        print("no models suppressed")
        return 0
    print(f"  {'model':<28} {'status':<7} {'since':<15} {'until':<16} detail")
    for m, e in sorted(entries.items()):
        live = "" if model_cooldown.is_suppressed(m) else "  (EXPIRED)"
        print(f"  {m:<28} {e.get('status','-'):<7} {_fmt(e.get('since')):<15} "
              f"{_fmt(e.get('until')):<16} {(e.get('detail') or '')[:60]}{live}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
