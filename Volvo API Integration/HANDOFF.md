# Volvo API Integration — Handoff Doc

## Goal

Replace a browser/token-scraping pipeline with a direct API fetcher that
produces the same 12-column "Standstill Tracking" report your Volvo Connect
portal shows, so it can run **unattended overnight** (Shift C: ~7 PM to
4–5 AM next day) with no browser open, no login, no token refresh.

The 12 columns (in report order):

```
Vehicle | Driver | Total time | Driving km | Diesel litres | Avg km/l |
Standstill % | Standstill time | Avg l/h | I-Shift % | Above economy % |
Engine load % | Coasting %
```

## Current architecture

Everything lives in **`D:\VolvoAutomation_APIv2\`**, fully separate from the
original working browser-based pipeline in `D:\VolvoAutomation\` (untouched,
still running on its normal schedule — nothing breaks if this stalls).

Files in that folder:
- `volvo_api_v2.py` — the main script (attached)
- `volvo_config.json` — credentials (Basic Auth: `client_id`:`client_secret`)
- `site_config.json` — copy of the existing site→VIN mapping file
- `inspect_snapshot.py` — small utility to print the shape of a dumped JSON file
- `test_scores.py` — one-off probe script used to discover the Score API (its job is done; kept for reference)

### Two APIs are in play, on the **same Basic Auth credentials** (username `4AE1335E81`):

1. **Vehicle Status API** — `https://api.volvotrucks.com/vehicle/vehiclestatuses`
   - Returns the **8 operational columns** (time, distance, fuel, standstill)
   - Query params: `starttime`, `stoptime` (full ISO timestamps, any granularity)
   - Returns cumulative/odometer-style counters + histogram "class" bins
     (see Data Model below)
   - **Paginates by time across the WHOLE FLEET** (~465 vehicles), not just
     your site — a full day can be 15,000+ records, ~170 pages of 100

2. **Score API** — `https://api.volvotrucks.com/score/scores`
   - Confirmed by Volvo support (see their email, quoted below) as the
     source for the 4 behaviour columns
   - **Only accepts whole dates** (`YYYY-MM-DD`), not timestamps — confirmed
     by testing; full-timestamp requests return `400 Invalid Date`
   - Supports `contentFilter=VEHICLES` (per-VIN) as well as `DRIVERS`
   - In practice, **only `I-Shift %` is actually usable from here** — see
     Behaviour Columns below for why the other 3 fields from this API were
     rejected in favor of computing them from the Vehicle Status API instead

**Volvo support's email (verbatim, for reference):**
> For the API account with Username: 4AE1335E81, the following driver
> behavior metrics: I-Shift Performance Time %, Above Economy %, Engine
> Load %, Coasting % can be retrieved using the Score API.
> Endpoint: `https://api.volvotrucks.com/score/scores?starttime=YYYY-MM-DD&stoptime=YYYY-MM-DD&contentFilter=DRIVERS&driverIdType=USB`
>
> 1. Volvo Group also provides a Driver Times API (driving/working/resting
>    times) — requires the separate Driver Times service to be enabled.
> 2. Reports can be scheduled for automatic email delivery (weekly/monthly)
>    — too coarse for per-shift use, but could be a nice bonus for a
>    management summary later.

## Data model (Vehicle Status API) — confirmed by real samples

Each snapshot (one per vehicle per ping) looks like:

```
vehicleStatusResponse.vehicleStatuses[] = {
  vin: str,
  createdDateTime: "2026-07-16T06:00:00.000Z",   # event time
  receivedDateTime: "2026-07-16T07:00:01.620Z",  # server receipt (used for pagination cursor)
  hrTotalVehicleDistance: int,      # METRES, cumulative odometer
  engineTotalFuelUsed: int,         # MILLILITRES, cumulative
  totalEngineHours: float,          # HOURS, cumulative engine-on time
  driver1Id: {oemDriverIdentification: {...}},
  accumulatedData: {                # only present on some snapshots! (see gotcha below)
    durationWheelbaseSpeedOverZero: int,   # SECONDS, cumulative "moving"
    durationWheelbaseSpeedZero: int,       # SECONDS, cumulative "standstill"
    ptoActiveClass: [ {label: "wheelbased speed >0"/"...=0", seconds, meters, milliLitres} ],
    accelerationClass: [ {from, to, seconds, meters, milliLitres} x13 ],   # acceleration histogram
    engineTorqueClass: [ {from, to, seconds, meters, milliLitres} x10 ],   # 0-100% torque bins
    engineSpeedClass: [ {from, to, seconds, meters, milliLitres} x10 ],    # rpm bins, 400-wide
    drivingWithoutTorqueClass: [ {seconds, meters} ],   # single bin, no from/to
    retarderTorqueClass, vehicleSpeedClass, accelerationPedalPositionClass,
    accelerationDuringBrakeClass, brakePedalCounterSpeedOverZero, ... (more, not currently used)
  }
}
```

**Critical gotcha:** snapshots come in two flavors —
- **TIMER snapshots** (hourly): carry full `accumulatedData` (all the class histograms)
- **EVENT snapshots** (ignition on/off etc.): only carry the top-level scalars
  (`hrTotalVehicleDistance`, `engineTotalFuelUsed`, `totalEngineHours`) — NO
  `accumulatedData` at all

The script's `window_diffs()` function handles this by interpolating **each
field independently** to the exact boundary time, only using snapshots that
actually carry that field. This was a real bug that got fixed (see History).

**Envelope/pagination shape** (also non-obvious, cost real time to discover):
```
GET /vehiclestatuses returns:
[  # top-level list (usually len=1 per page, but code tolerates more)
  {
    "vehicleStatusResponse": { "vehicleStatuses": [ ...100 records... ] },
    "moreDataAvailable": true,
    "requestServerDateTime": "..."
  }
]
```
Pagination is **NOT** by `lastVin` (a wrong early guess) — it's by re-issuing
the request with `starttime` advanced to the **latest `receivedDateTime`**
seen in the current page. This paginates across the **whole fleet**, so a
wide time window needs many pages.

## Site / vehicle config shape (confirmed)

`site_config.json` real shape (not flat — nested under `sites`):
```json
{
  "sites": {
    "Sasti": {
      "count": 9,
      "vehicles": [
        {"name": "V 502", "id": "...", "vin": "YV2XBZ0G5S8994987", "reg": null},
        ...
      ]
    }
  }
}
```
`name` is the column-1 display label (e.g. "V 502", "V-604" — inconsistent
spacing/hyphenation is original data, not a bug). `vin` is the join key to
the Vehicle Status API. **VIN-to-name mapping was independently verified
correct** against the portal's own export (see History — this was suspected
as a bug and ruled out).

## Behaviour columns — status per column

| Column | Source | Formula | Status |
|---|---|---|---|
| **I-Shift %** | Score API, `gearboxInAutoMode` field | direct value, day-granular | Range matches portal (~0–1.5%) — plausible, not rigorously proven |
| **Above economy %** | Vehicle Status API, `engineSpeedClass` | `metres in bins with from ≥ 1600 rpm` ÷ `total metres` | **Calibrated**: 28.8 vs portal's 28.89 (V 503, 16 Jul 07:00–13:00 IST) |
| **Engine load %** | Vehicle Status API, `engineTorqueClass` | `seconds in bins with from ≥ 80%` ÷ `driving_time_s` | Close: 7.1 vs portal's 7.68 — same truck/window. Not re-verified on other trucks yet |
| **Coasting %** | Vehicle Status API, `engineSpeedClass` | `metres in bins 400 ≤ rpm ≤ 800 (freewheeling)` ÷ `total metres` | Close: 4.6 vs portal's 5.19 — same caveat as above |

**Why not just use the Score API's own `coasting`/`engineLoad`/`aboveEconomy`
fields?** They exist in the response but turned out to be **0–100 ratings**,
not time-percentages — e.g. `engineLoad: 100.0` and `coasting: 90.0` for a
truck the portal shows at 7.68% and 5.19% respectively. Confirmed via the
`scores_B_vehicles_dates.json` dump from `test_scores.py`. Only
`gearboxInAutoMode` (→ I-Shift) landed in a range that matched the portal.

**These three formulas are validated on exactly ONE truck, ONE window.**
Before trusting them fleet-wide, calibrate against 2–3 more trucks (the
`--debug-vin` flag, multi-VIN comma-separated, exists for this — it prints
the raw class-bin diffs plus candidate computed values for any VIN you pass).

## Operational columns — status

| Window size | Result |
|---|---|
| 6-hour (07:00–13:00 IST) | **Mixed** — 2 of 9 trucks matched the portal almost exactly (V 590, V-603); others were off by −7% to +27%, in **both directions** (no consistent pattern) |
| Full day (00:00–24:00 IST) | **Every truck undershoots**, consistently, by roughly 1–2 hours of "Total time" and proportionally less km/fuel |

### Ruled-out theories (don't re-chase these — already tested and disproved)

1. ~~**Timezone mismatch**~~ — fixed early on; script now takes site-local
   (IST, `+05:30`) input by default via `parse_when()`, converts to UTC
   for the API. Confirmed working.
2. ~~**PTO (power take-off) work inflating distance/time**~~ — tested with
   an `--exclude-pto` flag that subtracts `ptoActiveClass` metrics. Result:
   **zero effect** — the tested trucks did 0 seconds/metres of PTO-while-
   moving in the test window. Not the cause. (Flag still in the code,
   harmless, but don't expect it to fix anything without evidence PTO time
   is actually present.)
3. ~~**VIN mapped to wrong truck**~~ — user pulled the portal's own export
   (`caliberMercantile-standstillTracking-....xlsx`, has a VIN column) and
   VINs matched our `site_config.json` **exactly** for all 3 checked trucks
   (V 503, V 590, V-604). Not a mapping bug.
4. ~~**Pagination cutting off before reaching the full day**~~ — raised
   `MAX_PAGES` from 200→3000 and added progress logging. Re-ran: fetch
   completed fully (cursor reached within 2.5 minutes of the requested
   stop), **zero change in output** — numbers were byte-for-byte identical
   to the pre-fix run. Not the cause. (The logging/higher cap is still
   worth keeping regardless — good instrumentation.)

### Leading unresolved theory (where to pick up)

**`Total time` may not equal the sum of the two counters it's built from.**
Currently: `Total time = driving_time_s (durationWheelbaseSpeedOverZero) +
standstill_time_s (durationWheelbaseSpeedZero)`. The truck also reports a
**third, independent counter**: `totalEngineHours` (cumulative engine-on
time). If `engine_hours > (driving_time_s + standstill_time_s)`, that gap is
direct proof the two wheelbase-speed counters don't cover 100% of the
truck's actual operating time (e.g. a third state, or telemetry gaps), which
would explain a systematic full-day undershoot cleanly.

**The next diagnostic step, not yet run:** a coverage-check debug dump was
just added (prints `engine_hours` vs `driving+standstill` sum, flags any gap
> 0.25h) but the run to actually see its output hadn't completed when this
handoff was written. The command to run:

```powershell
cd D:\VolvoAutomation_APIv2
python volvo_api_v2.py --site Sasti --start 2026-07-16T00:00:00 --stop 2026-07-17T00:00:00 --csv sasti_fullday3.csv --debug-vin YV2XBZ0G8S8995549,YV2XG30G7T8996904 > fullday_result3.txt 2>&1
```//
(VINs there are V 590 and V-604 — worst and middling offenders from the
full-day test.)

**If the coverage check shows a real gap:** the fix is likely switching
`Total time` to use `engine_hours` directly (or `max(engine_hours*3600,
driving+standstill)`), and re-deriving `Standstill %` accordingly.

**If the coverage check shows NO gap** (engine_hours ≈ driving+standstill,
consistent with a full day): the undershoot must be a **boundary
interpolation issue specific to long windows** — worth checking whether the
per-vehicle snapshot timeline actually has data near BOTH midnight boundaries
for the affected trucks (the restored debug printout shows first/last 5
snapshot timestamps — check these aren't hours away from midnight).

## Known-good calibration reference point

**Truck: V 503, VIN `YV2XBZ0G7S8994988`, window: 16 Jul 2026, 07:00–13:00 IST**

Portal ground truth (from portal screenshot):
```
Total time: 3:42 | Driving: 44.30 km | Diesel: 73.68 L | Avg km/l: 0.60
Standstill: 27.23% / 1:00 | Avg l/h: 19.92
I-Shift: 0.00 | Above economy: 28.89 | Engine load: 7.68 | Coasting: 5.19
```

Our computed raw diffs for the same truck/window (from debug dump):
```
distance_km=55.73  fuel_l=88.24  driving_time_s=11597  standstill_time_s=3988
engine_hours diff=4.578h
```
(Note: **still doesn't match portal even here** — 55.73 km vs 44.30 km,
un-resolved overshoot on this exact truck/window even after every fix so
far. This is the same underlying discrepancy as the "mixed direction" 6-hour
issue — treat this as the primary regression case to nail before declaring
victory on any one truck.)

Behaviour formulas against this same truck (these DID calibrate well):
```
Above economy: computed 28.8  vs portal 28.89  ✓
Engine load:   computed 7.1   vs portal 7.68    (close)
Coasting:      computed 4.6   vs portal 5.19    (close)
```

## Full-day ground truth (for cross-checking any future fix)

From the user's portal export (`caliberMercantile-standstillTracking-20260722.xlsx`,
date interval Jul 16 2026 12:00 AM–11:59 PM, Sasti site, 9 vehicles):

| Vehicle | VIN | Total time | Driving km | Diesel L | km/l | Standstill % | Standstill time | I-Shift % | Above eco % | Engine load % | Coasting % | Avg l/h |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| V-606 | YV2XG30G4T8996911 | 0:01 | 0.00 | 0.07 | 0.00 | 41.03 | 0:00 | – | 0.00 | 0.00 | 0.00 | 3.23 |
| V 590 | YV2XBZ0G8S8995549 | 15:53 | 204.29 | 308.11 | 0.66 | 25.38 | 4:01 | 0.02 | 17.87 | 6.47 | 6.93 | 19.40 |
| V-501 | YV2XBZ0GXS8994984 | 15:59 | 217.45 | 331.22 | 0.66 | 20.88 | 3:20 | 0.04 | 20.46 | 7.89 | 8.09 | 20.71 |
| V-604 | YV2XG30G7T8996904 | 16:00 | 198.94 | 310.20 | 0.64 | 23.50 | 3:45 | 0.00 | 18.86 | 6.51 | 4.81 | 19.37 |
| V-603 | YV2XG30G5T8996903 | 16:19 | 205.80 | 307.30 | 0.67 | 22.47 | 3:40 | 0.00 | 15.00 | 4.22 | 5.61 | 18.81 |
| V-605 | YV2XG30G9T8996905 | 16:27 | 193.84 | 305.23 | 0.64 | 22.59 | 3:42 | 0.27 | 17.09 | 4.40 | 6.60 | 18.55 |
| V 503 | YV2XBZ0G7S8994988 | 16:35 | 204.63 | 320.76 | 0.64 | 25.99 | 4:18 | 0.03 | 21.34 | 7.13 | 5.82 | 19.33 |
| V-500 | YV2XBZ0G6S8994982 | 16:38 | 210.62 | 350.75 | 0.60 | 24.23 | 4:01 | 0.03 | 12.87 | 7.50 | 4.56 | 21.07 |
| V 502 | YV2XBZ0G5S8994987 | 17:49 | 213.61 | 331.93 | 0.64 | 24.02 | 4:16 | 0.01 | 14.93 | 5.16 | 7.83 | 18.63 |

That same export's **"Details" tab** also has a **per-driver-segment
breakdown** (driver name, VIN-linked shift slices within the day) — useful
later for real driver-name attribution instead of "Unknown", not yet wired
into the script.

## Credentials

Basic Auth, username `4AE1335E81` (client_id), password is the client_secret
(shared with you separately / already in `volvo_config.json` on the user's
machine). **These have been circulating in plaintext across chat/email —
worth rotating in the Volvo Developer Portal once things stabilize.**
`volvo_config.json` is git/version-control-excluded by convention; never
hardcode credentials into the script itself (it deliberately reads from this
file or env vars).

## Everything that already works, don't re-derive

- Auth header format (Basic, base64 `id:secret`)
- Accept header for Vehicle Status API: `application/x.volvogroup.com.vehiclestatuses.v1.0+json; UTF-8` (auto-probed at runtime, cached)
- Accept header for Score API: `application/x.volvogroup.com.scores.v1.0+json; UTF-8`
- Score API pagination via `moreDataAvailableLink` (relative URL, needs `https://api.volvotrucks.com` prefixed and starttime/stoptime re-added if missing)
- CLI flags: `--site`, `--start`, `--stop` (site-local IST unless `Z`/offset given), `--csv`, `--raw` (dump JSON), `--debug-vin` (comma-separated VINs), `--no-scores`, `--exclude-pto`
- Report always emits all 9 site vehicles, even ones with no usable data in the window (blank row rather than dropping them)

## Immediate next action for whichever tool picks this up

1. Run the coverage-check command above (or re-run it if it changed) and
   read the `COVERAGE CHECK` line for both VINs.
2. If gap confirmed → fix `Total time`/`Standstill %` to account for it,
   re-validate against the full-day table above (all 9 trucks, one shot).
3. Once operational columns are solid, re-verify the 3 computed behaviour
   formulas (Above economy / Engine load / Coasting) on 2–3 more trucks
   using `--debug-vin` — currently proven on V 503 alone.
4. Only then move to: overnight Shift C window test (19:00→04:00/05:00,
   spans midnight — exercises both the day-splitting logic in
   `_shift_dates()` for the Score API AND the boundary interpolation, both
   already coded but never tested on an actual overnight window).
5. Driver-name wiring (optional/later) — the Details tab data above shows
   real driver names are obtainable per shift segment; not yet connected.
