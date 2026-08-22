#!/usr/bin/env python3
"""Watch a Haier AC's decoded status and print diffs as fields change.

Point it at the unit, press buttons on the remote, and watch which wire-model
fields flip — that is how every control position in this repo was found.

Usage:
  python3 status_watch.py --ip 192.0.2.50 --device XXXXXXXXXXXX \
      --uplus-id 00000000000000008080000000041410 --key LOCALKEY [--seconds 600]

Requires this repo's `vendor/haismart_hrdp` on PYTHONPATH (or pass --vendor-path).
"""

import argparse
import asyncio
import json
import sys
import time


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ip", required=True, help="AC IP address")
    p.add_argument("--device", required=True, help="device ID (MAC) from key fetch")
    p.add_argument("--key", required=True, help="local key")
    p.add_argument(
        "--uplus-id",
        default="00000000000000008080000000041410",
        help="uplus_id; the default matches compact-12 units",
    )
    p.add_argument(
        "--layout",
        default="compact12",
        help="expected status layout (classic/extended/compact12)",
    )
    p.add_argument(
        "--vendor-path",
        default=None,
        help="directory containing haismart_hrdp (default: ../vendor relative to this script)",
    )
    p.add_argument("--interval", type=float, default=2.0, help="poll interval")
    p.add_argument("--seconds", type=int, default=600, help="how long to watch")
    return p.parse_args()


def main():
    args = parse_args()

    vendor = args.vendor_path
    if vendor is None:
        import os

        vendor = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "vendor")
    sys.path.insert(0, vendor)

    from haismart_hrdp import async_read_status, parse_full_status

    def ts():
        return time.strftime("%H:%M:%S")

    async def run():
        prev = None
        end = time.time() + args.seconds
        print(ts(), f"watching {args.ip} for {args.seconds}s...", flush=True)
        while time.time() < end:
            try:
                blobs = await async_read_status(args.ip, args.device, args.key)
            except Exception as e:  # noqa: BLE001 - watcher must survive errors
                print(ts(), "ERR", repr(e), flush=True)
                await asyncio.sleep(args.interval)
                continue
            for b in blobs:
                d = parse_full_status(b, uplus_id=args.uplus_id)
                if not d or d.get("layout") != args.layout:
                    continue
                cur = {k: v for k, v in sorted(d.items())}
                if cur != prev:
                    if prev is None:
                        print(ts(), "BASELINE", json.dumps(cur, ensure_ascii=False), flush=True)
                    else:
                        diff = {k: (prev.get(k), v) for k, v in cur.items() if prev.get(k) != v}
                        print(ts(), "DIFF", json.dumps(diff, ensure_ascii=False), flush=True)
                    prev = cur
            await asyncio.sleep(args.interval)
        print(ts(), "watch end", flush=True)

    asyncio.run(run())


if __name__ == "__main__":
    main()
