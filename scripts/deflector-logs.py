#!/usr/bin/env python3
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at https://mozilla.org/MPL/2.0/.

"""Triage view over Deflector's logs.

Replaces the ad-hoc python one-liners that got written over and over during a
multi-hour outage: joining four log files by hand, on timestamps, because they
shared no identifier. Every record now carries a trace `id`, so this joins them
into one request-per-line view.

  deflector-logs.py                 last 20 requests
  deflector-logs.py --stalls        only requests with no completion recorded
  deflector-logs.py --outcome client_disconnect
  deflector-logs.py --id <trace>    everything known about one request
  deflector-logs.py --summary       outcome/model/kill rollup
  deflector-logs.py --follow        tail live

Reads only; never writes or deletes.

CAVEAT on historical data: records written before the `complete` event existed
have no completion, so they classify as stalled. For streaming requests that is
usually accurate (many were genuine hangs), but treat pre-upgrade rows as
"unknown" rather than proven stalls. This self-resolves as old files age out
under the 14-day metadata retention.
"""

from __future__ import annotations

import argparse
import collections
import datetime
import glob
import json
import os
import sys
import time

LOG_DIR = os.path.expanduser("~/.agentstop/logs")
STEMS = ("requests", "routing", "kills", "lifeos-escalations")


def _files(stem: str) -> list[str]:
    # dated files plus the sealed legacy ones, so history stays visible
    return sorted(glob.glob(os.path.join(LOG_DIR, f"{stem}-*.jsonl")))


def _read(stem: str, since: float | None = None) -> list[dict]:
    out = []
    for path in _files(stem):
        try:
            with open(path) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if since and rec.get("ts", 0) < since:
                        continue
                    rec["_src"] = stem
                    out.append(rec)
        except OSError:
            continue
    return out


def load(since: float | None = None) -> dict[str, dict]:
    """Group every record by trace id into one dict per request."""
    reqs: dict[str, dict] = collections.defaultdict(
        lambda: {"events": [], "routing": [], "kills": [], "lifeos": []})
    for stem, key in (("requests", "events"), ("routing", "routing"),
                      ("kills", "kills"), ("lifeos-escalations", "lifeos")):
        for rec in _read(stem, since):
            tid = rec.get("id")
            if tid:
                reqs[tid][key].append(rec)
    for tid, r in reqs.items():
        r["id"] = tid
        evs = {e.get("ev"): e for e in r["events"]}
        arrive, body, comp = evs.get("arrive"), evs.get("body_read"), evs.get("complete")
        r["ts"] = min((e.get("ts", 0) for e in r["events"]), default=0)
        r["client"] = (arrive or {}).get("client")
        r["path"] = (arrive or {}).get("path")
        r["model"] = (body or {}).get("model") or (comp or {}).get("model")
        r["in_bytes"] = (body or {}).get("bytes")
        headers = evs.get("headers")
        r["dur"] = (comp or {}).get("dur") or (headers or {}).get("dur")
        r["ttfb"] = (comp or {}).get("ttfb")
        r["out_bytes"] = (comp or {}).get("bytes_out")
        r["status"] = (headers or {}).get("status")

        # Classification. Two traps, both hit while building this:
        #
        #  1. "no complete event" alone does NOT mean stalled -- buffered
        #     endpoints (GET /api/tags, /pi/models.json) finish at `headers`,
        #     and records predating the complete event have none either.
        #  2. But `headers` alone does NOT mean finished, which is the more
        #     dangerous error: a StreamingResponse emits headers BEFORE its
        #     body, so the real outage looked like `headers 200 in 0.07s` and
        #     then silence for 300s with 0 bytes. Classifying that as "done"
        #     would blind exactly the incident this tool exists to surface.
        #
        # Every supervised request now emits `complete` -- streaming and
        # buffered alike -- so its absence after a `body_read` is a real stall
        # rather than a guess. (Using `body_read` alone as a "was streaming"
        # signal was wrong: it fires for both, and reported healthy `stream:
        # false` 200s as stalls.)
        #  3. And a request with no completion may simply still be RUNNING.
        #     Every supervised request emits `complete` -- kills included -- so
        #     absence means "not finished yet", which for a recent request is
        #     in-flight, not stalled. Reading the log mid-request otherwise
        #     reports healthy traffic as a stall; a 220KB prompt can sit in
        #     prefill for over a minute before its first byte. max_wall_seconds
        #     (900s) is the hard ceiling, so anything younger than that could
        #     legitimately still be going.
        if comp:
            r["outcome"] = comp.get("outcome")
            r["stalled"] = False
        elif body is not None and (time.time() - r["ts"]) < 900:
            r["outcome"] = "in-flight"
            r["stalled"] = False
        elif body is not None:
            r["outcome"] = None          # entered the handler, never finished
            r["stalled"] = True
        elif headers:
            r["outcome"] = f"done({headers.get('status')})"   # /api/tags etc.
            r["stalled"] = False
        else:
            r["outcome"] = None
            r["stalled"] = arrive is not None
    return dict(reqs)


def _fmt_ts(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


def _line(r: dict) -> str:
    outcome = r["outcome"] or ("STALLED/no-completion" if r["stalled"] else "-")
    mark = "!" if (r["stalled"] or (outcome or "").startswith(("kill", "error"))) else " "
    mark = "~" if outcome == "in-flight" else mark
    return (f"{mark} {_fmt_ts(r['ts'])}  {(r['client'] or '-'):<14} "
            f"{(r['model'] or '-'):<26} {outcome:<24} "
            f"ttfb={str(r['ttfb'] or '-'):<7} dur={str(r['dur'] or '-'):<7} "
            f"out={str(r['out_bytes'] or 0):>8}  {r['id']}")


def cmd_list(args, reqs):
    rows = sorted(reqs.values(), key=lambda r: r["ts"])
    if args.stalls:
        rows = [r for r in rows if r["stalled"]]
    if args.outcome:
        rows = [r for r in rows if (r["outcome"] or "") == args.outcome]
    if args.client:
        rows = [r for r in rows if r["client"] == args.client]
    rows = rows[-args.limit:]
    if not rows:
        print("no matching requests")
        return
    print(f"  {'time':<15} {'client':<14} {'model':<26} {'outcome':<24} "
          f"{'ttfb':<12} {'dur':<11} {'out':>8}  trace")
    for r in rows:
        print(_line(r))


def cmd_detail(args, reqs):
    r = reqs.get(args.id)
    if not r:
        print(f"no request with id {args.id}")
        return
    print(f"trace {r['id']}")
    print(f"  client {r['client']}   path {r['path']}   model {r['model']}")
    print(f"  outcome {r['outcome']}   ttfb {r['ttfb']}   dur {r['dur']}   "
          f"in {r['in_bytes']}B out {r['out_bytes']}B")
    for label, key in (("events", "events"), ("routing", "routing"),
                       ("kills", "kills"), ("lifeos", "lifeos")):
        for rec in sorted(r[key], key=lambda x: x.get("ts", 0)):
            body = {k: v for k, v in rec.items()
                    if k not in ("ts", "id", "_src")}
            print(f"  {_fmt_ts(rec.get('ts', 0))} [{label}] {body}")


def cmd_summary(args, reqs):
    outcomes = collections.Counter(
        r["outcome"] or ("stalled" if r["stalled"] else "unknown")
        for r in reqs.values())
    models = collections.Counter(r["model"] for r in reqs.values() if r["model"])
    kills = collections.Counter(
        k.get("reason") for r in reqs.values() for k in r["kills"])
    routes = collections.Counter(
        x.get("reason") for r in reqs.values() for x in r["routing"])

    total = sum(outcomes.values()) or 1
    print(f"requests: {total}\n")
    print("outcomes:")
    for k, v in outcomes.most_common():
        print(f"  {v:>6} ({v/total*100:5.1f}%)  {k}")
    if models:
        print("\nmodels:")
        for k, v in models.most_common(10):
            print(f"  {v:>6}  {k}")
    if kills:
        print("\nkills:")
        for k, v in kills.most_common():
            print(f"  {v:>6}  {k}")
    if routes:
        print("\nrouting decisions:")
        for k, v in routes.most_common(12):
            print(f"  {v:>6}  {k}")

    slow = sorted((r for r in reqs.values() if r["dur"]),
                  key=lambda r: r["dur"], reverse=True)[:5]
    if slow:
        print("\nslowest:")
        for r in slow:
            print(f"  {r['dur']:>8}s  {r['model']}  {r['id']}")


def cmd_follow(args, _):
    seen: set[str] = set()
    print("following (ctrl-c to stop)...")
    try:
        while True:
            reqs = load(since=time.time() - 3600)
            for r in sorted(reqs.values(), key=lambda x: x["ts"]):
                if r["id"] in seen or not r["outcome"]:
                    continue
                seen.add(r["id"])
                print(_line(r))
            time.sleep(2)
    except KeyboardInterrupt:
        pass


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--hours", type=float, default=24,
                    help="how far back to read (default 24)")
    ap.add_argument("--stalls", action="store_true",
                    help="only requests with no completion recorded")
    ap.add_argument("--outcome", help="filter by exact outcome")
    ap.add_argument("--client", help="filter by client IP")
    ap.add_argument("--id", help="show everything known about one trace id")
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--follow", action="store_true")
    args = ap.parse_args()

    if not os.path.isdir(LOG_DIR):
        print(f"log dir not found: {LOG_DIR}", file=sys.stderr)
        return 1

    since = time.time() - args.hours * 3600
    if args.follow:
        return cmd_follow(args, None) or 0
    reqs = load(since=None if args.id else since)
    if args.id:
        cmd_detail(args, reqs)
    elif args.summary:
        cmd_summary(args, reqs)
    else:
        cmd_list(args, reqs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
