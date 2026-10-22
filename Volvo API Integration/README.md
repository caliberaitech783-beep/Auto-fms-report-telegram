# Volvo Trucks direct API report

This project generates the Volvo Connect Standstill Tracking report directly
from Volvo's Vehicle Status and Score APIs. It does not require Chrome, an open
portal session, or browser-token scraping.

## Required local configuration

Keep credentials out of source control. Either put `volvo_config.json` and
`site_config.json` beside `volvo_api_v2.py`, or point to the existing files:

```powershell
$env:VOLVO_CONFIG_FILE = 'D:\VolvoAutomation_APIv2\volvo_config.json'
$env:VOLVO_SITE_CONFIG_FILE = 'D:\VolvoAutomation_APIv2\site_config.json'
```

Install the dependency:

```powershell
py -m pip install -r requirements.txt
```

## Generate reports

Full-day Sasti calibration report (times without an offset are treated as IST):

```powershell
py volvo_api_v2.py --site Sasti `
  --start 2026-07-16T00:00:00 --stop 2026-07-17T00:00:00 `
  --csv sasti_fullday.csv `
  --debug-vin YV2XBZ0G8S8995549,YV2XG30G7T8996904
```

Overnight Shift C, 7 PM to 5 AM next day:

```powershell
py volvo_api_v2.py --site Sasti `
  --start 2026-07-22T19:00:00 --stop 2026-07-23T05:00:00 `
  --csv sasti_shift_c.csv
```

The report includes all configured site vehicles, even when a vehicle has no
usable data. Volvo may rate-limit a fleet-wide historical fetch; the client now
honours `Retry-After` and retries transient HTTP/network failures.

## Current validation status

- Authentication, response envelope, time pagination, IST conversion, VIN
  mapping, and class-aware boundary interpolation are implemented.
- I-Shift comes from the day-granular Score API. Above-economy, engine-load,
  and coasting percentages are derived from Vehicle Status histogram counters.
- The supplied handoff records an unresolved difference between some API-derived
  operational totals and the Volvo portal export. Do not replace the existing
  browser report in production until a successful full-day debug run is compared
  with the portal ground truth in `HANDOFF.md`.

Run offline checks with:

```powershell
py -m unittest -v test_volvo_api.py
```

