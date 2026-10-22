# Volvo APIv2 automated reports

This project generates browser-free Volvo PDF reports from the Volvo APIs and
optionally queues or sends them through the WhatsApp API.

## Schedule and same-day recovery

The Windows task `Volvo APIv2 PDF Reports` retains the daily report triggers:

- 10:30
- 11:45
- 15:00
- 17:00
- 18:45

It also has a Windows-login trigger. Every invocation plans only the scheduled
cutoffs on the current local date that are less than or equal to the current
time. It never automatically plans a previous date or a future cutoff.

Before an API request, the runner calculates the exact expected PDF filename
for every due site/cutoff. Existing files are skipped. All remaining same-day
cutoffs are recovered in chronological order using one fleet-status fetch.

Example: logging in at 16:00 recovers missing 10:30, 11:45 and 15:00 outputs
for today. It does not generate the 17:00 or 18:45 outputs and does not inspect
yesterday for missing reports.

## Vehicle eligibility

A vehicle is included only when both conditions are met before sorting,
averages, KPI counts and summaries:

- total duration is at least 30 minutes;
- travelled distance is at least 15 km (exactly 15 km is included).

The PDF legend states both exclusion rules.

## Automatic Volvo Connect group synchronization

Before planning reports, the runner reads the newest file matching
`Caliber Mercantile - assets - *.xlsx` in the Windows Downloads folder. The
export must contain the `Name`, `Groups`, `Service Status`,
`Service Subscriptions` and `VIN` columns.

Every asset assigned to a configured public vehicle group is mapped to that
site. An asset with no recognized group is mapped to `Unknown sites` when its
subscriptions include `Fuel & Environment`. Ungrouped assets without that
package are excluded. API data availability and the normal 30-minute and
15-km rules still determine which mapped vehicles appear in each PDF.

The last valid export is cached as `asset_group_master.xlsx`. Every accepted
change creates a timestamped backup under `output\config_backups` and updates
`site_config.json` atomically. A missing or malformed new export leaves the
last valid mapping in service instead of clearing site assignments.

`Unknown sites` uses the 05:00/13:00/21:00 shift pattern until the vehicle is
assigned to its correct public group.

## Installation

From an Administrator PowerShell prompt, run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\VolvoAutomation_APIv2\Set_Volvo_API_Report_Times.ps1
```

The installer replaces only `Volvo APIv2 PDF Reports`, preserving the five
daily times and adding the login trigger. The task runs the hidden
`run_api_reports.bat` action with overlapping instances ignored.

## Tests

Run from the project directory:

```powershell
python -m unittest discover -s tests -v
```

Tests do not call Volvo or WhatsApp APIs and do not generate production PDFs.

## Important files

- `api_report_runner.py`: scheduling recovery, duplicate checks and API flow.
- `api_pdf_report.py`: eligibility filtering, calculations and PDF rendering.
- `volvo_api_v2.py`: Volvo API access and vehicle calculations.
- `asset_group_sync.py`: public-group/package import and Unknown-sites routing.
- `asset_group_master.xlsx`: cached last valid Volvo Connect asset export.
- `shifts.json`: site shift boundaries.
- `Set_Volvo_API_Report_Times.ps1`: Windows task installer.
- `output\pdf`: generated reports and WhatsApp queue.
- `logs`: scheduled-run and application logs.
