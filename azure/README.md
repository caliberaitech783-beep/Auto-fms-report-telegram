# Isolated Azure report worker

This is a separate Python 3.12 Azure Function App on Flex Consumption, with its
own resource group and private Standard LRS storage. It does not deploy into or
change existing apps or their hosting plans. `build_package.py` builds a ZIP from
an explicit source allowlist. Live credentials are supplied through app settings,
never committed or included in the source ZIP.

The monitored timer runs every five minutes. Python uses Asia/Kolkata for report
dates. A renewable Blob lease also prevents manual runs from overlapping the
timer; it expires automatically after a crashed worker stops renewing it.
Due cutoffs are 03:00, 09:00, 11:30, 16:00, 18:00 and 23:00 IST. Every report
covers its current shift's start through that cutoff. The 03:00 partial report
starts on the previous date at 21:00 (20:00 for Jayant). It is not a full C-shift
report; no extra 04:00/05:00 final report is scheduled. Majri uses 05:00/13:00/21:00
shift starts, matching the supplied shift table. Generated PDFs and
delivery receipts survive host restarts in the private `fleet-report-state`
container. Successful uploads checkpoint receipts immediately. A lost Telegram
response can still cause a duplicate on retry.

## Required app settings

- `SCANIA_CONFIG_JSON`, `VOLVO_CONFIG_JSON`: credential configuration objects.
- `SCANIA_SITES_ZLIB_B64`, `VOLVO_SITES_ZLIB_B64`: zlib-compressed, base64-encoded
  UTF-8 site configuration JSON. Plain `*_SITES_JSON` is also supported.
- `TELEGRAM_BOT_TOKEN`: sender credentials.
- `TELEGRAM_ROUTES_JSON`: object mapping filename substrings to destination chat
  IDs: Dhoptala, Majri, Sasti, Gouri, Dudhichua, Jayant. Both brands use the same
  routes; all Dudhichua variants go to DCH FMS and all Jayant variants to Jayant FMS.
  Unmapped reports stay in private storage; they are never sent to a fallback group.
  `TELEGRAM_CHAT_ID` supports legacy single-group mode when routes are absent.
- `REPORT_SCHEDULE_START`: IST ISO activation timestamp. Earlier cutoffs are
  excluded from generation and delivery, preventing Testing history from being
  replayed into site groups. Changing this setting clears the old schedule's
  completion markers/backlog while preserving today's used generation allowance.
- `REPORT_START_DATE`: earliest eligible PDF date, in `YYYY-MM-DD` format.
- `REPORTS_ENABLED`: leave `false` until cutover is ready; set `true` to activate.
- `MAX_GENERATION_SECONDS_PER_DAY`: live deployment `21600` (360 minutes) across
  both brands; the conservative fallback remains `1800`. A new
  generation attempt reserves its timeout against this allowance before starting;
  unused time is returned after the process exits. A crashed attempt retains its
  reservation to prevent runaway retries. With incremental collection enabled,
  each attempt is capped at 90 seconds for collection, 180 seconds for Scania,
  and 300 seconds for Volvo. Retries for a scheduled batch are bounded at
  240 seconds for Scania and 480 seconds for Volvo. Every later cutoff retains
  240 seconds of shared generation runtime. Background collection has a further
  1,800-second/day ceiling and cannot consume those reserves.
- `VOLVO_INCREMENTAL_CACHE=true`: collect fleet history every half hour and five
  minutes before each report cutoff. The next report fetches only new records
  with a ten-minute overlap for late arrivals. Every downloaded page commits its
  records and received-time cursor. A timeout resumes from that cursor on the
  next attempt; incomplete history is never treated as a complete report.
  A consistent compressed SQLite snapshot survives restarts at
  `cache/volvo/fleet.sqlite3.gz`. Snapshot history is trimmed to three days;
  it contains operational data and must stay private. It is independent of the
  PDF lifecycle policy. Inventory refreshes hourly; driver names every six hours.
- `VOLVO_AUTO_SITE_PREFIX=true`: discover new API-visible VINs and add a vehicle
  whose Volvo Connect customer name begins with one exact configured report site
  followed by a separator, e.g. `Sasti V850`, `Jayant East V851`, or
  `Dudhichua West V852`. Existing mapped VINs retain their site. API activation
  alone does not provide group membership: the live Vehicle API exposes no
  Volvo Connect group field. Unprefixed or ambiguous new VINs remain unassigned
  in the private cache; their count is visible in `status.json`. The API account
  must also be authorized to see each new VIN. No portal passwords are needed.
- `AzureWebJobsStorage`: this app's own host and report storage connection string,
  provisioned by Azure CLI during app creation.

The app uses no always-ready instances and creates no Application Insights resource.
The daily generation allowance limits report API work, not total Azure billing.
Storage, timer execution, data transfer and other charges still apply. The live
resource-group budget is ₹4,500/month with an actual-spend alert at ₹1,000
(22.2222% of the budget); budgets are alerts rather than hard caps. The private
PDF prefixes `fleet-report-state/pdf/scania/` and `fleet-report-state/pdf/volvo/`
have an enabled storage lifecycle rule deleting blobs more than 10 days after
their last modification. Delivery state and other prefixes are excluded.
If the daily generation allowance is exhausted,
remaining generation waits until the next date. Delivery retries continue.
Incomplete dates are retained in a per-fleet backlog. With incremental collection,
current reports take priority and older dates use spare runtime. History older
than the three-day cache needs an explicit API backfill. PDFs already generated for those dates
are restored to avoid recreating and resending them during catch-up.

Each completed brand is delivered immediately, with a durable receipt after each
Telegram acknowledgement. A delayed batch posts one notice to its affected site
groups after ten minutes. State preserves the configured daily generation limit;
deployment never resets a spent allowance. API outages, source-data latency,
Telegram outages, or an exhausted allowance can still delay delivery. The
five-minute timer begins work at each listed cutoff, with PDFs following processing.

## Build and verification

```powershell
python azure/build_package.py path/to/deployment.zip
python -m unittest discover -s azure -p 'test_*.py' -v
python -m unittest discover -s automation -p 'test_*.py' -v
```

Cloud lifecycle tests use fake storage and fleet APIs. Remote build and live fleet
API verification must still pass in Azure before calling deployment complete.

For cutover, leave `REPORTS_ENABLED=false`, seed the private container with today's
PDFs and a consistent SQLite backup of desktop receipts, and verify configuration.
Only then disable the desktop Telegram delivery task, enable cloud reporting and
verify `status.json` plus Telegram receipts. Keep desktop generation tasks intact.
If cloud verification fails, disable the new cloud worker and re-enable desktop
Telegram delivery. This avoids changes to existing Azure applications.
