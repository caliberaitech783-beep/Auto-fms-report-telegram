#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_scores.py — probe the Volvo Score API before integrating it.

Answers three questions in one run:
  1. Which Accept header does /score/scores use?
  2. Does it accept full timestamps (shift windows) or only whole dates?
  3. Does it support contentFilter=VEHICLES (per-vehicle) as well as DRIVERS?

Reads credentials from volvo_config.json / env vars, same as volvo_api_v2.py.
Dumps every successful response to scores_<n>.json and prints a structure
outline of the first success.

Usage:
    python test_scores.py
    python test_scores.py --date 2026-07-16     (day to probe; default shown)
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

SCORES_URL = "https://api.volvotrucks.com/score/scores"
CONFIG_FILE = Path(__file__).with_name("volvo_config.json")
TIMEOUT = 60

ACCEPT_CANDIDATES = [
    "application/x.volvogroup.com.scores.v1.0+json; UTF-8",
    "application/x.volvogroup.com.scores.v1.0+json",
    "application/x.volvogroup.com.score.v1.0+json; UTF-8",
    "application/x.volvogroup.com.scores.v2.0+json; UTF-8",
    "application/json",
]


def creds() -> tuple[str, str]:
    cid = os.environ.get("VOLVO_CLIENT_ID")
    cs = os.environ.get("VOLVO_CLIENT_SECRET")
    if cid and cs:
        return cid, cs
    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if cfg.get("client_id") and cfg.get("client_secret"):
            return cfg["client_id"], cfg["client_secret"]
    sys.exit("No credentials (env vars or volvo_config.json).")


def outline(node, depth=0, label="", max_depth=8):
    pad = "  " * depth
    if depth > max_depth:
        print(f"{pad}{label}…")
        return
    if isinstance(node, dict):
        print(f"{pad}{label}{{}} dict ({len(node)} keys)")
        for k, v in node.items():
            outline(v, depth + 1, f"{k}: ", max_depth)
    elif isinstance(node, list):
        print(f"{pad}{label}[] list (len={len(node)})")
        if node:
            outline(node[0], depth + 1, "[0] ", max_depth)
    else:
        s = repr(node)
        print(f"{pad}{label}{type(node).__name__} = {s[:60]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default="2026-07-16",
                    help="day to probe (YYYY-MM-DD)")
    args = ap.parse_args()
    day = args.date
    next_day = None
    # naive next-day calc for stoptime
    from datetime import date, timedelta
    y, m, d = map(int, day.split("-"))
    next_day = (date(y, m, d) + timedelta(days=1)).isoformat()

    cid, cs = creds()
    auth = {"Authorization": "Basic "
            + base64.b64encode(f"{cid}:{cs}".encode()).decode()}

    # --- 1) find the Accept header using the exact example format ---
    base_params = {"starttime": day, "stoptime": next_day,
                   "contentFilter": "DRIVERS", "driverIdType": "USB"}
    accept = None
    for cand in ACCEPT_CANDIDATES:
        r = requests.get(SCORES_URL, headers={**auth, "Accept": cand},
                         params=base_params, timeout=TIMEOUT)
        print(f"[accept probe] {cand!r:60s} -> {r.status_code}")
        if r.status_code == 401:
            sys.exit("401 Unauthorized - Score API may need separate enabling.")
        if r.status_code not in (406, 415):
            accept = cand
            break
    if accept is None:
        sys.exit("No Accept header variant worked for the Score API.")
    print(f"\n[OK] Accept header: {accept}\n")
    headers = {**auth, "Accept": accept}

    # --- 2+3) probe variants: time granularity x content filters ---
    variants = [
        ("A_drivers_dates",
         {"starttime": day, "stoptime": next_day,
          "contentFilter": "DRIVERS", "driverIdType": "USB"}),
        ("B_vehicles_dates",
         {"starttime": day, "stoptime": next_day,
          "contentFilter": "VEHICLES"}),
        ("C_drivers_datetime",
         {"starttime": f"{day}T01:30:00Z", "stoptime": f"{day}T07:30:00Z",
          "contentFilter": "DRIVERS", "driverIdType": "USB"}),
        ("D_vehicles_datetime",
         {"starttime": f"{day}T01:30:00Z", "stoptime": f"{day}T07:30:00Z",
          "contentFilter": "VEHICLES"}),
        ("E_nofilter_dates",
         {"starttime": day, "stoptime": next_day}),
    ]

    first_success = None
    for name, params in variants:
        try:
            r = requests.get(SCORES_URL, headers=headers, params=params,
                             timeout=TIMEOUT)
        except requests.RequestException as e:
            print(f"[{name}] network error: {e}")
            continue
        body_preview = (r.text or "")[:200].replace("\n", " ")
        print(f"[{name}] HTTP {r.status_code}  {body_preview}")
        if r.status_code == 200:
            try:
                data = r.json()
            except ValueError:
                print(f"[{name}] 200 but non-JSON body")
                continue
            out = Path(f"scores_{name}.json")
            out.write_text(json.dumps(data, indent=2), encoding="utf-8")
            print(f"[{name}] saved -> {out}")
            if first_success is None:
                first_success = (name, data)

    if first_success:
        name, data = first_success
        print(f"\n=== structure of first success ({name}) ===")
        outline(data)
    else:
        print("\nNo variant returned 200 - paste this whole output back.")


if __name__ == "__main__":
    main()
