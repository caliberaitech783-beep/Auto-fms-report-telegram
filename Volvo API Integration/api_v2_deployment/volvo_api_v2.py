#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
volvo_api_v2.py
================
Direct-API replacement for the old browser/token data source.

Pulls the 12-column Standstill / Vehicle-Utilisation report straight from
Volvo's official Vehicle API using Basic Auth (client id + secret). No Chrome,
no login, no token scraping. Works unattended at any hour -> ideal for the
overnight Shift C window (19:00 -> next-day 04:00/05:00).

How it works
------------
The API keeps its own rolling history and returns ~hourly cumulative snapshots
per vehicle. For any shift window we:
    1. Query /vehiclestatuses for starttime -> stoptime.
    2. For each VIN, take the FIRST snapshot at/after start and the LAST
       snapshot before stop (== "now" for a live report).
    3. Diff the two cumulative counters to get the values for that window.
    4. Compute the 12 report columns from the diffs.

Because it's server-side history, a Shift C report generated at 03:00 just asks
for "21:00 yesterday -> 03:00 today" and diffs -> the machine can even have been
off earlier. That's the whole point of moving off the browser.

USAGE
-----
Import into the existing pipeline (recommended):
    from volvo_api_v2 import get_shift_report
    rows = get_shift_report(site="Sasti", start="2026-07-16T19:00:00Z",
                            stop="2026-07-17T05:00:00Z")

Standalone validation (prove numbers match the portal before cutover):
    python volvo_api_v2.py --site Sasti --start 2026-07-16T07:00:00Z \
                           --stop 2026-07-16T13:00:00Z --csv out.csv

CONFIG / CREDENTIALS
--------------------
Credentials are NOT hardcoded here. The script reads them, in order, from:
    1. Environment variables VOLVO_CLIENT_ID and VOLVO_CLIENT_SECRET, else
    2. volvo_config.json  (see volvo_config.example.json)
Keep volvo_config.json out of version control. Rotate the credentials in the
portal if they've been circulated in any shared document.

>>> BEFORE FIRST REAL RUN: check the one TODO block (FIELD_MAP) against a real
    sample snapshot. Everything else is complete.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")


# ---------------------------------------------------------------------------
# API constants
# ---------------------------------------------------------------------------
BASE_URL = "https://api.volvotrucks.com/vehicle"
STATUSES_PATH = "/vehiclestatuses"          # window query: ?starttime=..&stoptime=..
VEHICLES_PATH = "/vehicles"                 # vehicle list (for driver-name route)

# --- Score API (behaviour columns; confirmed by Volvo support) ---------------
SCORES_URL = "https://api.volvotrucks.com/score/scores"
VEHICLES_ACCEPT = "application/x.volvogroup.com.vehicles.v1.0+json; UTF-8"
SCORES_ACCEPT = "application/x.volvogroup.com.scores.v1.0+json; UTF-8"  # confirmed by probe
# The Score API is DAY-granular (YYYY-MM-DD only): behaviour columns therefore
# reflect the calendar day(s) spanned by the shift, not the exact shift window.
#
# Which score field feeds which report column. NEEDS ONE CALIBRATION CHECK:
# compare one Sasti truck's values in scores_B_vehicles_dates.json against the
# portal's full-day view; adjust these four names if they don't line up.
# Score fields such as gearboxInAutoMode are 0-100 ratings, not the portal's
# shift-specific time percentages. I-Shift is therefore calculated from the
# Vehicle Status transmissionModeSeconds POWER counter in compute_row().
SCORE_MAP = {}

# Each Volvo resource has its own media type. The /vehiclestatuses endpoint uses
# a different one from /vehicles, so we probe these in order and use whichever the
# server accepts (anything other than 406/415). The winner is printed so it can be
# pinned as the single header once known.
ACCEPT_HEADER_CANDIDATES = [
    "application/x.volvogroup.com.vehiclestatuses.v1.0+json; UTF-8",
    "application/x.volvogroup.com.vehiclestatuses.v1.0+json",
    "application/x.volvogroup.com.vehiclestatus.v1.0+json; UTF-8",
    "application/x.volvogroup.com.vehiclestatuses.v2.0+json; UTF-8",
    "application/x.volvogroup.com.vehiclestatuses.v1.1+json; UTF-8",
    "application/x.volvogroup.com.vehicles.v1.0+json; UTF-8",
    "application/vnd.volvogroup.com.vehiclestatuses.v1.0+json",
    "application/json",
]

CONFIG_FILE = Path(os.environ.get(
    "VOLVO_CONFIG_FILE", Path(__file__).with_name("volvo_config.json")
))
SITE_CONFIG_FILE = Path(os.environ.get(
    "VOLVO_SITE_CONFIG_FILE", Path(__file__).with_name("site_config.json")
))

REQUEST_TIMEOUT = 60  # seconds
MAX_HTTP_RETRIES = 6


def _api_get(url: str, *, headers: dict, params: dict | None = None):
    """GET with bounded retries for Volvo throttling and transient failures."""
    for attempt in range(MAX_HTTP_RETRIES + 1):
        try:
            resp = requests.get(url, headers=headers, params=params,
                                timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            if attempt >= MAX_HTTP_RETRIES:
                raise
            delay = min(2 ** attempt, 30)
            print(f"[http] network error; retrying in {delay}s "
                  f"({attempt + 1}/{MAX_HTTP_RETRIES})")
            time.sleep(delay)
            continue

        if resp.status_code != 429 and resp.status_code < 500:
            return resp
        if attempt >= MAX_HTTP_RETRIES:
            return resp

        retry_after = resp.headers.get("Retry-After")
        try:
            delay = max(float(retry_after), 1.0) if retry_after else min(2 ** attempt, 30)
        except ValueError:
            delay = min(2 ** attempt, 30)
        delay = min(delay, 60.0)
        print(f"[http] HTTP {resp.status_code}; retrying in {delay:g}s "
              f"({attempt + 1}/{MAX_HTTP_RETRIES})")
        time.sleep(delay)

    raise RuntimeError("unreachable")


# ===========================================================================
#  >>>>>>>>>>>>>>>>>>>>>>>>  THE ONLY TODO IN THIS FILE  <<<<<<<<<<<<<<<<<<<<<
# ===========================================================================
# Map each report value to the JSON key in a /vehiclestatuses snapshot.
# Paste one real snapshot, read off the actual key names, and fill these in.
# Dotted paths are supported for nested objects, e.g. "engine.totalFuelLitres".
#
# All the counters below are assumed to be CUMULATIVE (odometer-style), so the
# window value = value_at_last_snapshot - value_at_first_snapshot. That is how
# rFMS / Volvo vehicle data normally behaves. If any field turns out to be an
# instantaneous percentage instead of a cumulative counter, tell me and I'll
# switch that one to distance-weighted averaging instead of diffing.
FIELD_MAP = {
    # --- identity --- (confirmed from the real sample)
    "vin":                 "vin",
    "driver_id":           "driver1Id.oemDriverIdentification",   # optional

    # --- cumulative operational counters --- (confirmed real keys/units)
    "distance_km":         "hrTotalVehicleDistance",  # METRES -> km (see UNITS)
    "fuel_l":              "engineTotalFuelUsed",      # MILLILITRES -> litres
    "engine_hours":        "totalEngineHours",         # HOURS (engine-on)
    "driving_time_s":      "accumulatedData.durationWheelbaseSpeedOverZero",  # SECONDS moving
    "standstill_time_s":   "accumulatedData.durationWheelbaseSpeedZero",      # SECONDS at standstill

    # --- behaviour columns (the 4 proprietary ones) ---
    # PENDING: these live inside accumulatedData as histogram *class* arrays,
    # not as flat scalars. Filling these needs the class structures (next
    # inspector run) + one portal cross-check to lock the formula. Left blank
    # for now so the operational columns validate first.
    "coasting_class":      "accumulatedData.drivingWithoutTorqueClass",  # no-torque rolling
    "engine_load_class":   "accumulatedData.engineTorqueClass",          # -> Engine load %
    "engine_speed_class":  "accumulatedData.engineSpeedClass",           # -> Above economy % & Coasting %
    "accel_class":         "accumulatedData.accelerationClass",          # (analysis)
    "pto_class":           "accumulatedData.ptoActiveClass",             # PTO work (portal appears to exclude)
    "transmission_mode_class":
        "accumulatedData.volvoGroupAccumulated.transmissionModeSeconds",
}

# Unit conversions to normalise raw counters to report units.
UNITS = {
    "distance_km":       0.001,   # metres -> km
    "fuel_l":            0.001,   # millilitres -> litres
    "engine_hours":      1.0,     # already hours
    "driving_time_s":    1.0,     # seconds
    "standstill_time_s": 1.0,     # seconds
}
# ===========================================================================
#  >>>>>>>>>>>>>>>>>>>>>>>>>>>>  END TODO BLOCK  <<<<<<<<<<<<<<<<<<<<<<<<<<<<<
# ===========================================================================


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------
def load_credentials() -> tuple[str, str]:
    """Return (client_id, client_secret) from env or config file. Never hardcoded."""
    cid = os.environ.get("VOLVO_CLIENT_ID")
    csecret = os.environ.get("VOLVO_CLIENT_SECRET")
    if cid and csecret:
        return cid, csecret

    if CONFIG_FILE.exists():
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        cid = cfg.get("client_id")
        csecret = cfg.get("client_secret")
        if cid and csecret:
            return cid, csecret

    sys.exit(
        "No credentials found.\n"
        "  Set VOLVO_CLIENT_ID / VOLVO_CLIENT_SECRET, or create volvo_config.json:\n"
        '    {"client_id": "YOUR_ID", "client_secret": "YOUR_SECRET"}\n'
        "  (copy volvo_config.example.json). Keep it out of version control."
    )


def auth_header(client_id: str, client_secret: str) -> dict:
    token = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    return {"Authorization": f"Basic {token}"}  # Accept added per-call by the probe


# ---------------------------------------------------------------------------
# Site / VIN mapping
# ---------------------------------------------------------------------------
def load_site_config() -> dict:
    """site_config.json already contains VIN for every truck, keyed per site."""
    if not SITE_CONFIG_FILE.exists():
        sys.exit(f"site_config.json not found next to the script: {SITE_CONFIG_FILE}")
    return json.loads(SITE_CONFIG_FILE.read_text(encoding="utf-8"))


def _site_node(site: str, site_config: dict) -> dict | None:
    """Return the vehicle-container node for a site across supported shapes."""
    # Real shape: {"sites": {"Sasti": {"count": N, "vehicles": [...]}}}
    if isinstance(site_config.get("sites"), dict):
        return site_config["sites"].get(site)
    # Fallbacks: {"Sasti": {...}} or {"Sasti": [...]}
    if site in site_config:
        return site_config[site]
    return None


def _site_vehicles(site: str, site_config: dict) -> list[dict]:
    """Flat list of vehicle dicts for a site."""
    node = _site_node(site, site_config)
    if isinstance(node, dict) and isinstance(node.get("vehicles"), list):
        return [v for v in node["vehicles"] if isinstance(v, dict)]
    if isinstance(node, list):
        return [v for v in node if isinstance(v, dict)]
    # flat list with a site field
    if isinstance(site_config.get("vehicles"), list):
        return [v for v in site_config["vehicles"]
                if isinstance(v, dict) and v.get("site") == site]
    return []


def vins_for_site(site: str, site_config: dict) -> set[str]:
    """VINs for one site. Matches your file: {"sites": {"<site>": {"vehicles": [...]}}}."""
    vehicles = _site_vehicles(site, site_config)
    if not vehicles:
        available = ", ".join((site_config.get("sites") or {}).keys()) or "(none)"
        sys.exit(f"Could not find site '{site}' in site_config.json. "
                 f"Available sites: {available}")
    return {v.get("vin") for v in vehicles if v.get("vin")}


def meta_for_vin(vin: str, site: str, site_config: dict,
                 display_names: dict[str, str] | None = None) -> dict:
    """Return report metadata, preferring the live Volvo Connect display name."""
    for v in _site_vehicles(site, site_config):
        if v.get("vin") == vin:
            return {
                "registration": (display_names or {}).get(vin)
                                or v.get("name") or v.get("reg")
                                or v.get("registration") or v.get("plate"),
                "driver": v.get("driver") or v.get("driver_name") or "Unknown",
            }
    return {"registration": None, "driver": "Unknown"}


def fetch_vehicle_display_names(auth: dict) -> dict[str, str]:
    """Current Volvo Connect customer display names keyed by VIN."""
    url = BASE_URL + VEHICLES_PATH
    headers = {**auth, "Accept": VEHICLES_ACCEPT}
    names: dict[str, str] = {}
    last_vin: str | None = None

    for page_no in range(1, 100):
        params = {"lastVin": last_vin} if last_vin else {}
        try:
            resp = _api_get(url, headers=headers, params=params)
            if resp.status_code == 401:
                print("[vehicles] 401 Unauthorized; using local site_config names")
                return {}
            resp.raise_for_status()
        except requests.RequestException as exc:
            print(f"[vehicles] metadata request failed ({exc}); "
                  "using local site_config names")
            return {}

        data = resp.json()
        body = data.get("vehicleResponse", data) if isinstance(data, dict) else {}
        vehicles = (body.get("vehicles") or []) if isinstance(body, dict) else []
        for item in vehicles:
            if not isinstance(item, dict) or not item.get("vin"):
                continue
            display_name = str(item.get("customerVehicleName") or "").strip()
            if display_name:
                names[item["vin"]] = display_name

        if not data.get("moreDataAvailable") or not vehicles:
            break
        next_vin = vehicles[-1].get("vin")
        if not next_vin or next_vin == last_vin:
            print("[vehicles] pagination stopped because lastVin did not advance")
            break
        last_vin = next_vin

    print(f"[vehicles] loaded {len(names)} current Volvo Connect display names "
          f"over {page_no} page(s)")
    return names


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------
_RESOLVED_ACCEPT: str | None = None  # cached once a candidate succeeds


def _resolve_accept(url: str, params: dict, auth: dict) -> str:
    """Probe ACCEPT_HEADER_CANDIDATES; return the first the server accepts."""
    global _RESOLVED_ACCEPT
    if _RESOLVED_ACCEPT:
        return _RESOLVED_ACCEPT

    last_status = None
    for accept in ACCEPT_HEADER_CANDIDATES:
        headers = {**auth, "Accept": accept}
        try:
            resp = _api_get(url, headers=headers, params=params)
        except requests.RequestException as e:
            sys.exit(f"Network error contacting API: {e}")
        if resp.status_code == 401:
            sys.exit("401 Unauthorized - check client id/secret (or rotate them).")
        if resp.status_code in (406, 415):
            last_status = resp.status_code
            continue  # wrong media type; try the next candidate
        # anything else (200, or a 4xx that isn't a media-type problem) means the
        # Accept header itself was acceptable
        _RESOLVED_ACCEPT = accept
        print(f"[accept] using: {accept}")
        return accept

    sys.exit(
        f"No Accept header worked (last status {last_status}). "
        "Paste the working curl from the earlier session, or check the exact "
        "media type on the portal's /vehiclestatuses docs, and I'll pin it."
    )


def _extract_statuses(page) -> tuple[list[dict], bool]:
    """
    Pull the status list and moreDataAvailable flag out of one response page.
    Real shape: top-level is a list whose element(s) are page dicts:
        {"vehicleStatusResponse": {"vehicleStatuses": [...]},
         "moreDataAvailable": bool, "requestServerDateTime": "..."}
    Also tolerates the page dict appearing bare (not wrapped in a list).
    """
    statuses: list[dict] = []
    more = False

    def _from_page(p: dict):
        nonlocal more
        vsr = p.get("vehicleStatusResponse") or p
        lst = vsr.get("vehicleStatuses") if isinstance(vsr, dict) else None
        if isinstance(lst, list):
            statuses.extend(lst)
        if p.get("moreDataAvailable"):
            more = True

    if isinstance(page, list):
        for p in page:
            if isinstance(p, dict):
                _from_page(p)
    elif isinstance(page, dict):
        _from_page(page)

    return statuses, more


def fetch_statuses(start: str, stop: str, auth: dict) -> list[dict]:
    """
    GET /vehiclestatuses?starttime=..&stoptime.. -> flat list of status dicts
    across ALL pages.

    rFMS paginates /vehiclestatuses by TIME, not by VIN, and returns the WHOLE
    FLEET per page (not just the site being reported on): when a page returns
    moreDataAvailable=true, re-request with starttime advanced to the latest
    receivedDateTime seen. Records are deduped on (vin, createdDateTime).

    A wide window across a large fleet can be tens of thousands of records, so
    this prints progress and clearly flags if the page cap is hit before the
    fleet-wide cursor reaches `stop` (which would silently truncate later
    hours of the window for every vehicle).
    """
    url = BASE_URL + STATUSES_PATH
    accept = _resolve_accept(url, {"starttime": start, "stoptime": stop}, auth)
    headers = {**auth, "Accept": accept}

    out: list[dict] = []
    seen: set[tuple] = set()
    cursor = start
    MAX_PAGES = 3000  # safety cap only; a full fleet-day is a few hundred pages

    pages = 0
    for pages in range(1, MAX_PAGES + 1):
        resp = _api_get(url, headers=headers,
                        params={"starttime": cursor, "stoptime": stop})
        if resp.status_code == 401:
            sys.exit("401 Unauthorized - check client id/secret (or rotate them).")
        resp.raise_for_status()

        statuses, more = _extract_statuses(resp.json())
        if not statuses:
            break

        latest_received = None
        for s in statuses:
            key = (s.get("vin"), s.get("createdDateTime"))
            if key not in seen:
                seen.add(key)
                out.append(s)
            rd = s.get("receivedDateTime")
            if rd and (latest_received is None or rd > latest_received):
                latest_received = rd

        if pages % 25 == 0:
            print(f"[statuses] page {pages}, {len(out)} records so far, "
                  f"cursor at {cursor}")

        if not more:
            break
        if not latest_received or latest_received <= cursor:
            break  # no forward progress -> stop rather than loop
        cursor = latest_received
    else:
        print(f"[statuses] WARNING: hit the {MAX_PAGES}-page safety cap "
              f"before the fleet cursor reached '{stop}' (stopped at "
              f"'{cursor}'). Later hours of the window are likely MISSING "
              f"for every vehicle -> undercounted totals. Raise MAX_PAGES "
              f"or narrow the window.")

    print(f"[statuses] fetched {len(out)} records over {pages} page(s); "
         f"cursor reached {cursor} (requested through {stop})")
    return out


# ---------------------------------------------------------------------------
# Snapshot helpers
# ---------------------------------------------------------------------------
def _get(obj: dict, dotted_key: str):
    """Fetch a possibly-nested value by dotted path; None if missing."""
    cur = obj
    for part in dotted_key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _timestamp(snap: dict) -> datetime | None:
    """Best-effort parse of a snapshot's timestamp across common key names."""
    for key in ("createdDateTime", "receivedDateTime", "timestamp",
                "createdAt", "time", "eventTimeStamp"):
        raw = snap.get(key)
        if raw:
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def group_by_vin(snaps: list[dict]) -> dict[str, list[dict]]:
    by_vin: dict[str, list[dict]] = {}
    vin_key = FIELD_MAP["vin"]
    for s in snaps:
        vin = _get(s, vin_key)
        if vin:
            by_vin.setdefault(vin, []).append(s)
    for vin in by_vin:
        by_vin[vin].sort(key=lambda s: _timestamp(s) or datetime.min.replace(
            tzinfo=timezone.utc))
    return by_vin


# ---------------------------------------------------------------------------
# Boundary interpolation
# ---------------------------------------------------------------------------
# The API mixes hourly TIMER snapshots (full accumulatedData) with event
# snapshots (odometer/fuel/engine-hours only). Also snapshots land on UTC hour
# marks (:30 IST), never exactly on shift boundaries. Instead of picking one
# snapshot per boundary, we interpolate each cumulative counter to the EXACT
# boundary time, per field, using only snapshots that carry that field.
CLAMP_TOLERANCE_S = 90 * 60   # max gap to clamp to a one-sided snapshot

SCALAR_FIELDS = ["distance_km", "fuel_l", "driving_time_s",
                 "standstill_time_s", "engine_hours"]
CLASS_FIELDS = ["coasting_class", "engine_load_class",
                "engine_speed_class", "accel_class", "pto_class",
                "transmission_mode_class"]
_BIN_VALUE_KEYS = ("seconds", "meters", "milliLitres", "value")


def _lerp(t0, v0, t1, v1, t) -> float:
    if t1 == t0:
        return v0
    f = (t - t0).total_seconds() / (t1 - t0).total_seconds()
    return v0 + (v1 - v0) * f


def _bracket(points: list[tuple[datetime, object]], t: datetime):
    """(before, after) points around t; points sorted ascending by time."""
    before = after = None
    for ts, v in points:
        if ts <= t:
            before = (ts, v)
        else:
            after = (ts, v)
            break
    return before, after


def _boundary_scalar(snaps_sorted: list[dict], t: datetime,
                     logical: str) -> float | None:
    """Value of one cumulative counter at exactly time t (interpolated)."""
    points = []
    for s in snaps_sorted:
        ts = _timestamp(s)
        v = _val(s, logical)
        if ts is not None and v is not None:
            points.append((ts, v))
    before, after = _bracket(points, t)
    if before and after:
        return _lerp(before[0], before[1], after[0], after[1], t)
    if before and (t - before[0]).total_seconds() <= CLAMP_TOLERANCE_S:
        return before[1]
    if after and (after[0] - t).total_seconds() <= CLAMP_TOLERANCE_S:
        return after[1]
    return None


def _keyed_bins(snap: dict, logical: str) -> dict | None:
    lst = _class_list(snap, logical)
    if not lst:
        return None
    out = {}
    for b in lst:
        out[(b.get("from"), b.get("to"), b.get("label"))] = b
    return out


def _boundary_bins(snaps_sorted: list[dict], t: datetime,
                   logical: str) -> dict | None:
    """Interpolated per-bin cumulative values of one histogram class at time t.
    Returns {bin_key: {value_key: float}} or None if the class isn't available."""
    points = []
    for s in snaps_sorted:
        ts = _timestamp(s)
        kb = _keyed_bins(s, logical)
        if ts is not None and kb is not None:
            points.append((ts, kb))
    before, after = _bracket(points, t)

    if before and after:
        (t0, a), (t1, b) = before, after
        out = {}
        for k in a.keys() & b.keys():
            vals = {}
            for f in _BIN_VALUE_KEYS:
                if f in a[k] and f in b[k]:
                    vals[f] = _lerp(t0, a[k][f] or 0, t1, b[k][f] or 0, t)
            out[k] = vals
        return out
    for side, gap_ok in ((before, before and (t - before[0]).total_seconds()
                          <= CLAMP_TOLERANCE_S),
                         (after, after and (after[0] - t).total_seconds()
                          <= CLAMP_TOLERANCE_S)):
        if side and gap_ok:
            return {k: {f: (v.get(f) or 0) for f in _BIN_VALUE_KEYS if f in v}
                    for k, v in side[1].items()}
    return None


def window_diffs(snaps_sorted: list[dict], start_dt: datetime,
                 stop_dt: datetime) -> dict | None:
    """All counter diffs for [start, stop], interpolated to exact boundaries.
    Returns None when nothing usable exists for the window."""
    if not snaps_sorted:
        return None

    w: dict = {"_start": start_dt, "_stop": stop_dt}
    any_scalar = False
    for logical in SCALAR_FIELDS:
        a = _boundary_scalar(snaps_sorted, start_dt, logical)
        b = _boundary_scalar(snaps_sorted, stop_dt, logical)
        if a is None or b is None or b - a < 0:
            w[logical] = None
        else:
            w[logical] = b - a
            any_scalar = True

    for logical in CLASS_FIELDS:
        a = _boundary_bins(snaps_sorted, start_dt, logical)
        b = _boundary_bins(snaps_sorted, stop_dt, logical)
        bins = []
        if a and b:
            for k in a.keys() & b.keys():
                row = {"from": k[0], "to": k[1], "label": k[2]}
                ok = True
                for f in _BIN_VALUE_KEYS:
                    if f in a[k] and f in b[k]:
                        d = b[k][f] - a[k][f]
                        if d < -1e-6:
                            ok = False
                            break
                        row[f] = max(d, 0.0)
                if ok:
                    bins.append(row)
        w[logical + "_bins"] = bins

    return w if any_scalar else None


def _val(snap: dict, logical: str) -> float | None:
    raw = _get(snap, FIELD_MAP[logical])
    if raw is None:
        return None
    try:
        return float(raw) * UNITS.get(logical, 1.0)
    except (TypeError, ValueError):
        return None


def _diff(first: dict, last: dict, logical: str) -> float | None:
    a, b = _val(first, logical), _val(last, logical)
    if a is None or b is None:
        return None
    d = b - a
    return d if d >= 0 else None  # negative diff => counter reset; treat as gap


# ---------------------------------------------------------------------------
# Behaviour columns: computed from accumulatedData histogram classes
# ---------------------------------------------------------------------------
# Formulas derived by calibrating V 503 (16 Jul, 07:00-13:00 IST) against the
# portal. Tuning knobs below; verify on 2-3 more trucks before cutover.
#   Above economy % = metres at rpm >= ECONOMY_RPM_MIN / total metres
#                     (V 503: 28.8 vs portal 28.89 -> locked)
#   Engine load %   = seconds at torque >= ENGINE_LOAD_MIN_PCT / moving seconds
#                     (V 503: 7.14 vs portal 7.68 -> close, verify)
#   Coasting %      = metres freewheeling (idle rpm while moving) / total metres
#                     (V 503: 4.6 vs portal 5.19 -> candidate, verify)
#   I-Shift %       = POWER transmission-mode seconds / total shift time
ENGINE_LOAD_MIN_PCT = 80.0    # engineTorqueClass bins with from >= this
ECONOMY_RPM_MIN     = 1600.0  # engineSpeedClass bins with from >= this
IDLE_RPM_MIN        = 400.0   # freewheel band for Coasting
IDLE_RPM_MAX        = 800.0


def _class_list(snap: dict, logical: str) -> list[dict]:
    lst = _get(snap, FIELD_MAP[logical])
    return lst if isinstance(lst, list) else []


def _class_diff(first: dict, last: dict, logical: str) -> list[dict]:
    """Per-bin diff of a cumulative histogram class between two snapshots.

    Bins are matched by (from, to, label) and MUST exist in BOTH snapshots —
    some event-triggered snapshots omit classes, and diffing against a missing
    baseline would wrongly yield the lifetime cumulative value. Negative diffs
    (counter reset) are skipped."""
    first_bins = _class_list(first, logical)
    last_bins = _class_list(last, logical)
    if not first_bins or not last_bins:
        return []  # class absent in one snapshot -> can't diff this window

    def keyed(lst):
        d = {}
        for b in lst:
            k = (b.get("from"), b.get("to"), b.get("label"))
            d[k] = b
        return d

    a, b = keyed(first_bins), keyed(last_bins)
    out = []
    for k, bin_last in b.items():
        if k not in a:
            continue  # bin missing from baseline -> skip, don't fake a zero
        bin_first = a[k]
        row = {"from": bin_last.get("from"), "to": bin_last.get("to"),
               "label": bin_last.get("label")}
        ok = True
        for f in ("seconds", "meters", "milliLitres"):
            if f in bin_last:
                d = (bin_last.get(f) or 0) - (bin_first.get(f) or 0)
                if d < 0:
                    ok = False
                    break
                row[f] = d
        if ok:
            out.append(row)
    return out


def _sum_field(bins: list[dict], field: str, min_from: float | None = None,
               max_to: float | None = None) -> float:
    total = 0.0
    for b in bins:
        if min_from is not None:
            f = b.get("from")
            if f is None or f < min_from:
                continue
        if max_to is not None:
            t = b.get("to")
            if t is None or t > max_to:
                continue
        total += b.get(field) or 0
    return total



    if part is None or not whole:
        return None
    return round(part / whole * 100.0, 1)


def _pct(part: float | None, whole: float | None) -> float | None:
    if part is None or not whole:
        return None
    return round(part / whole * 100.0, 1)


def _hhmm(seconds: float | None) -> str:
    if seconds is None:
        return ""
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}"


def compute_row(vin: str, w: dict, meta: dict,
                exclude_pto: bool = False) -> dict:
    """Build one report row from the interpolated window diffs (window_diffs()).

    exclude_pto: subtract PTO-active operation (water spraying / tipping) from
    distance/time/fuel. Hypothesis under test: the portal excludes PTO work,
    which explains why non-PTO trucks match exactly and PTO trucks overshoot.
    """
    distance_km  = w.get("distance_km")
    fuel_l       = w.get("fuel_l")
    driving_s    = w.get("driving_time_s")
    standstill_s = w.get("standstill_time_s")
    engine_h     = w.get("engine_hours")

    # --- PTO split (label 'wheelbased speed >0' = moving, else stationary) ---
    pto_bins = w.get("pto_class_bins") or []
    pto_move = next((b for b in pto_bins
                     if b.get("label") and ">" in str(b["label"])), None)
    pto_still = next((b for b in pto_bins
                      if b.get("label") and ">" not in str(b["label"])), None)

    if exclude_pto:
        if pto_move:
            if distance_km is not None:
                distance_km = max(distance_km - (pto_move.get("meters") or 0) / 1000.0, 0.0)
            if driving_s is not None:
                driving_s = max(driving_s - (pto_move.get("seconds") or 0), 0.0)
            if fuel_l is not None:
                fuel_l = max(fuel_l - (pto_move.get("milliLitres") or 0) / 1000.0, 0.0)
        if pto_still:
            if standstill_s is not None:
                standstill_s = max(standstill_s - (pto_still.get("seconds") or 0), 0.0)
            if fuel_l is not None:
                fuel_l = max(fuel_l - (pto_still.get("milliLitres") or 0) / 1000.0, 0.0)

    # Total time = moving + standstill (self-consistent with Standstill %).
    total_s = None
    if driving_s is not None and standstill_s is not None:
        total_s = driving_s + standstill_s
    elif engine_h is not None:
        total_s = engine_h * 3600.0

    total_h = (total_s / 3600.0) if total_s else None

    # --- behaviour columns (formulas calibrated against the portal, V 503) ---
    distance_m = (distance_km * 1000.0) if distance_km is not None else None

    # Above economy % = metres at rpm >= ECONOMY_RPM_MIN / total metres
    rpm_bins = w.get("engine_speed_class_bins") or []
    above_m = _sum_field(rpm_bins, "meters", min_from=ECONOMY_RPM_MIN) \
        if rpm_bins else None
    above_economy_pct = _pct(above_m, distance_m)

    # Engine load % = seconds at torque >= ENGINE_LOAD_MIN_PCT / moving seconds
    torque_bins = w.get("engine_load_class_bins") or []
    load_s = _sum_field(torque_bins, "seconds", min_from=ENGINE_LOAD_MIN_PCT) \
        if torque_bins else None
    engine_load_pct = _pct(load_s, driving_s)

    # Coasting % = freewheel metres (idle rpm while moving) / total metres
    coast_m = _sum_field(rpm_bins, "meters", min_from=IDLE_RPM_MIN,
                         max_to=IDLE_RPM_MAX) if rpm_bins else None
    coasting_pct = _pct(coast_m, distance_m)

    # I-Shift Performance Time % = time in POWER transmission mode / Total time.
    # Volvo stores transmission mode duration in a cumulative list using the
    # field name "value" (seconds), rather than the usual "seconds" field.
    transmission_bins = w.get("transmission_mode_class_bins") or []
    power_s = sum((b.get("value") or 0) for b in transmission_bins
                  if str(b.get("label") or "").upper() == "POWER") \
        if transmission_bins else None
    i_shift_pct = _pct(power_s, total_s)

    return {
        "Vehicle":         meta.get("registration") or vin,
        "Driver":          meta.get("driver", "Unknown"),
        # operational (validated basis)
        "Total time":      _hhmm(total_s),
        "Driving km":      round(distance_km, 1) if distance_km is not None else "",
        "Diesel litres":   round(fuel_l, 1) if fuel_l is not None else "",
        "Avg km/l":        round(distance_km / fuel_l, 2)
                           if distance_km is not None and fuel_l else "",
        "Standstill %":    _pct(standstill_s, total_s) if total_s else "",
        "Standstill time": _hhmm(standstill_s),
        "Avg l/h":         round(fuel_l / total_h, 2)
                           if fuel_l is not None and total_h else "",
        # behaviour
        "I-Shift %":       i_shift_pct if i_shift_pct is not None else "",
        "Above economy %": above_economy_pct if above_economy_pct is not None else "",
        "Engine load %":   engine_load_pct if engine_load_pct is not None else "",
        "Coasting %":      coasting_pct if coasting_pct is not None else "",
    }


# ---------------------------------------------------------------------------
# Public entry point (this is what the existing pipeline calls)
# ---------------------------------------------------------------------------
SITE_UTC_OFFSET = "+05:30"   # local time of the sites (IST). Portal shows local.
PRE_WINDOW_MIN = 120         # fetch this many minutes before start so a
                             # baseline snapshot at/before shift start exists


def parse_when(s: str) -> datetime:
    """Parse a time string; naive times are treated as SITE LOCAL (IST).
    Accepts '2026-07-16T07:00:00', '2026-07-16 07:00', with 'Z' or an offset."""
    s = s.strip().replace(" ", "T")
    if s.endswith("Z"):
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:  # naive -> site local
        dt = datetime.fromisoformat(f"{s}{SITE_UTC_OFFSET}")
    return dt


def _utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _shift_dates(start_dt: datetime, stop_dt: datetime) -> tuple[str, str]:
    """Inclusive site-local calendar dates for the day-granular Score API.

    Volvo accepts a same-day request (starttime == stoptime) for an in-progress
    day and rejects a future stop date. When a report ends exactly at midnight,
    that boundary belongs to the preceding reporting day.
    """
    from datetime import timedelta
    tz = start_dt.tzinfo
    d0 = start_dt.astimezone(tz).date()
    d1 = stop_dt.astimezone(tz).date()
    local_stop = stop_dt.astimezone(tz)
    if local_stop.time() == datetime.min.time() and stop_dt > start_dt:
        d1 -= timedelta(days=1)
    return d0.isoformat(), max(d0, d1).isoformat()


def fetch_scores(start_date: str, stop_date: str, auth: dict) -> dict[str, dict]:
    """Per-vehicle behaviour scores from the Score API -> {vin: scores_dict}.
    Day-granular; follows moreDataAvailableLink pagination."""
    headers = {**auth, "Accept": SCORES_ACCEPT}
    url = SCORES_URL
    params: dict | None = {"starttime": start_date, "stoptime": stop_date,
                           "contentFilter": "VEHICLES"}
    out: dict[str, dict] = {}
    for _ in range(100):  # safety cap
        try:
            resp = _api_get(url, headers=headers, params=params)
        except requests.RequestException as e:
            print(f"[scores] network error ({e}); behaviour columns will be blank")
            return out
        if resp.status_code != 200:
            print(f"[scores] HTTP {resp.status_code}; behaviour columns will be blank")
            return out
        data = resp.json()
        body = data.get("vuScoreResponse", data) if isinstance(data, dict) else {}
        for v in body.get("vehicles") or []:
            vin, scores = v.get("vin"), v.get("scores")
            if vin and isinstance(scores, dict):
                out[vin] = scores
        link = body.get("moreDataAvailableLink") if body.get("moreDataAvailable") \
            else None
        if not link:
            break
        # link is relative like '/score/scores?lastVin=...&contentFilter=VEHICLES'
        url = "https://api.volvotrucks.com" + link if link.startswith("/") else link
        if "starttime=" not in url:
            sep = "&" if "?" in url else "?"
            url += f"{sep}starttime={start_date}&stoptime={stop_date}"
        params = None
    return out


def get_shift_report(site: str, start: str, stop: str,
                     debug_vin: str | None = None,
                     with_scores: bool = True,
                     exclude_pto: bool = False) -> list[dict]:
    """
    Return a list of 12-column rows for one site over [start, stop].

    start/stop: ISO strings. Naive times are SITE LOCAL (IST, +05:30) to match
    the portal; append Z or an offset for UTC/explicit zones.

    Operational columns: exact shift window (interpolated snapshot diffs).
    Behaviour columns: Score API values for the shift's calendar day(s) —
    the Score API is day-granular, so these are daily, not shift-window.
    """
    from datetime import timedelta

    start_dt, stop_dt = parse_when(start), parse_when(stop)
    fetch_start = start_dt - timedelta(minutes=PRE_WINDOW_MIN)

    cid, csecret = load_credentials()
    auth = auth_header(cid, csecret)
    site_config = load_site_config()
    display_names = fetch_vehicle_display_names(auth)

    wanted_vins = vins_for_site(site, site_config)
    snaps = fetch_statuses(_utc_z(fetch_start), _utc_z(stop_dt), auth)
    by_vin = group_by_vin(snaps)

    scores_by_vin: dict[str, dict] = {}
    if with_scores:
        d0, d1 = _shift_dates(start_dt, stop_dt)
        scores_by_vin = fetch_scores(d0, d1, auth)
        print(f"[scores] {len(scores_by_vin)} vehicles with scores "
              f"for {d0} -> {d1} (day-granular)")

    debug_vins = {v.strip() for v in (debug_vin or "").split(",") if v.strip()}

    rows: list[dict] = []
    for vin in sorted(wanted_vins):
        w = window_diffs(by_vin.get(vin, []), start_dt, stop_dt)
        meta = meta_for_vin(vin, site, site_config, display_names)
        if w is None:
            row = {"Vehicle": meta.get("registration") or vin,
                   "Driver": meta.get("driver", "Unknown"),
                   "Total time": "", "Driving km": "", "Diesel litres": "",
                   "Avg km/l": "", "Standstill %": "", "Standstill time": "",
                   "Avg l/h": "", "I-Shift %": "", "Above economy %": "",
                   "Engine load %": "", "Coasting %": ""}
        else:
            if vin in debug_vins:
                _debug_dump(vin, w, by_vin.get(vin, []),
                            meta.get("registration"))
            row = compute_row(vin, w, meta, exclude_pto=exclude_pto)

        # I-Shift % from the Score API (day-granular)
        sc = scores_by_vin.get(vin)
        if sc:
            for col, field in SCORE_MAP.items():
                val = sc.get(field)
                if val is not None:
                    row[col] = round(float(val), 1)
        rows.append(row)
    return rows


def _debug_dump(vin: str, w: dict, snaps_sorted: list[dict],
                name: str | None = None) -> None:
    """Print interpolated window diffs + candidate computations for one truck
    so formulas can be verified against the portal."""
    print(f"\n===== DEBUG {vin} ({name or '?'}) =====")
    print(f"window: {w.get('_start')} -> {w.get('_stop')}")

    print(f"snapshots in range ({len(snaps_sorted)} total, first/last 5 shown):")
    to_show = snaps_sorted[:5] + (["..."] if len(snaps_sorted) > 10 else []) \
        + snaps_sorted[-5:] if len(snaps_sorted) > 5 else snaps_sorted
    for s in to_show:
        if s == "...":
            print("    ...")
            continue
        has_acc = "yes" if isinstance(s.get("accumulatedData"), dict) else "no "
        print(f"    {_timestamp(s)}  acc={has_acc}")

    for logical in SCALAR_FIELDS:
        print(f"  {logical:20s} window diff = {w.get(logical)}")

    # Coverage check: do the two wheelbase-speed counters (driving+standstill)
    # actually account for the truck's engine-on time? A gap here means
    # 'Total time' (their sum) is structurally undercounting.
    drv, still, eng_h = w.get("driving_time_s"), w.get("standstill_time_s"), \
        w.get("engine_hours")
    if drv is not None and still is not None and eng_h is not None:
        wheelbase_h = (drv + still) / 3600.0
        gap_h = eng_h - wheelbase_h
        print(f"  COVERAGE CHECK: engine_hours={eng_h:.2f}h vs "
              f"(driving+standstill)={wheelbase_h:.2f}h  "
              f"-> gap={gap_h:+.2f}h "
              f"{'<-- SUSPECT: unexplained gap' if abs(gap_h) > 0.25 else '(consistent)'}")

    for logical in CLASS_FIELDS:
        bins = w.get(logical + "_bins") or []
        print(f"  {logical} ({len(bins)} bins):")
        for b in sorted(bins, key=lambda x: (x.get('from') is None,
                                             x.get('from') or 0)):
            print(f"    from={b.get('from')} to={b.get('to')} "
                  f"label={b.get('label')} seconds={round(b.get('seconds'), 1) if b.get('seconds') is not None else None} "
                  f"meters={round(b.get('meters'), 1) if b.get('meters') is not None else None} "
                  f"milliLitres={round(b.get('milliLitres'), 1) if b.get('milliLitres') is not None else None}")
    # candidate computations, raw vs PTO-excluded, for portal comparison
    for tag, ex in (("raw", False), ("PTO-excluded", True)):
        r = compute_row(vin, w, {"registration": name or vin,
                                 "driver": "?"}, exclude_pto=ex)
        print(f"  [{tag}] time={r['Total time']} km={r['Driving km']} "
              f"L={r['Diesel litres']} km/l={r['Avg km/l']} "
              f"still%={r['Standstill %']} still={r['Standstill time']} "
              f"l/h={r['Avg l/h']} aboveEco={r['Above economy %']} "
              f"load={r['Engine load %']} coast={r['Coasting %']}")
    print("===== END DEBUG =====\n")


# ---------------------------------------------------------------------------
# Standalone validation runner
# ---------------------------------------------------------------------------
COLUMNS = ["Vehicle", "Driver", "Total time", "Driving km", "Diesel litres",
           "Avg km/l", "Standstill %", "Standstill time", "Avg l/h",
           "I-Shift %", "Above economy %", "Engine load %", "Coasting %"]


def _print_table(rows: list[dict]) -> None:
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) if rows
              else len(c) for c in COLUMNS}
    line = " | ".join(c.ljust(widths[c]) for c in COLUMNS)
    print(line)
    print("-" * len(line))
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in COLUMNS))


def _write_csv(rows: list[dict], path: str) -> None:
    import csv
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLUMNS})
    print(f"\nWrote {len(rows)} rows -> {path}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Volvo direct-API shift report (v2).")
    ap.add_argument("--site", required=True, help="Site name as in site_config.json")
    ap.add_argument("--start", required=True,
                    help="Window start, SITE LOCAL time (IST), e.g. 2026-07-16T07:00:00. "
                         "Append Z for UTC.")
    ap.add_argument("--stop", required=True,
                    help="Window stop, SITE LOCAL time (IST), e.g. 2026-07-16T13:00:00")
    ap.add_argument("--csv", help="Optional path to also write a CSV")
    ap.add_argument("--raw", help="Debug: dump the raw snapshot JSON to this path")
    ap.add_argument("--debug-vin", help="Print raw class diffs for one VIN (calibration)")
    ap.add_argument("--no-scores", action="store_true",
                    help="Skip the Score API (behaviour columns from class bins/blank)")
    ap.add_argument("--exclude-pto", action="store_true",
                    help="Subtract PTO-active work from distance/time/fuel "
                         "(testing whether the portal excludes PTO)")
    args = ap.parse_args()

    if args.raw:
        from datetime import timedelta
        cid, csecret = load_credentials()
        s = parse_when(args.start) - timedelta(minutes=PRE_WINDOW_MIN)
        snaps = fetch_statuses(_utc_z(s), _utc_z(parse_when(args.stop)),
                               auth_header(cid, csecret))
        Path(args.raw).write_text(json.dumps(snaps, indent=2), encoding="utf-8")
        print(f"Dumped {len(snaps)} snapshots -> {args.raw}")

    rows = get_shift_report(args.site, args.start, args.stop,
                            debug_vin=args.debug_vin,
                            with_scores=not args.no_scores,
                            exclude_pto=args.exclude_pto)
    print(f"\n{args.site}   {args.start} -> {args.stop}  (site local {SITE_UTC_OFFSET})\n")
    _print_table(rows)
    if args.csv:
        _write_csv(rows, args.csv)


if __name__ == "__main__":
    main()
