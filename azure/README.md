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
- `MAX_GENERATION_SECONDS_PER_DAY`: default `1800` across both brands. A new
  generation attempt reserves its timeout against this allowance before starting;
  unused time is returned after the process exits. A crashed attempt retains its
  reservation to prevent runaway retries. Each process is capped at 900 seconds.
- `AzureWebJobsStorage`: this app's own host and report storage connection string,
  provisioned by Azure CLI during app creation.

The app uses no always-ready instances and creates no Application Insights resource.
The daily generation allowance limits report API work, not total Azure billing.
Storage, timer execution, data transfer and other charges still apply. An ₹800
resource-group budget alert supports the user's ₹1,000 monthly target; budgets
are alerts rather than hard caps. If the daily generation allowance is exhausted,
remaining generation waits until the next date. Delivery retries continue.
Incomplete dates are retained in a per-fleet backlog and generated oldest first
when the next day's allowance is available. PDFs already generated for those dates
are restored to avoid recreating and resending them during catch-up.

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
