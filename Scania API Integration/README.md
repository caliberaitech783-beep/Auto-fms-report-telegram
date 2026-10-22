# Scania browser-free API reports

This separate setup replaces the Scania browser/export workflow with direct
Scania Data Access calls and creates A4 portrait shift PDFs.

## What is implemented

- Scania challenge-response authentication (client ID, HMAC-SHA256 secret).
- rFMS v4 `/vehicles` and `/vehiclestatuses` integration with retry and pagination.
- Exact shift-boundary interpolation of cumulative counters.
- The workbook's **Scania** rules:
  - exclude engine running time below 00:30;
  - sort by idling percentage descending;
  - flag km/l below fleet average and litres/hour above fleet average;
  - flag idling above 28%, Power distance above 30 km, Power share above 1%,
    and coasting below 20%.
- A4 portrait PDF with thresholds at the bottom of the report.
- Daily automated report times:
  A1 10:30, A2 11:45, B1 15:00, B2 17:00, and B3 18:45.
  Overnight C reports remain supported for manual runs but are not scheduled.
- Automatic current-date catch-up at Windows login and each scheduled run:
  missing PDFs for today are generated once their report time has passed.
  Previous dates are not generated automatically, and existing PDFs are skipped.
- Failed scheduled runs (for example, temporary internet or DNS outages) are
  retried up to six times at five-minute intervals.
- A site that fails while another site succeeds is retried again within the
  same run after the remaining sites have been processed.
- A scheduled-task installer and a guarded script to disable the old browser task.

## Configure

1. Copy `scania_config.example.json` to `scania_config.json`.
2. Set your Scania client ID and matching **secret key** in `client_id` and
   `client_secret`. Never commit `scania_config.json`.
3. Check `site_config.json`. Registration numbers were transcribed from the supplied
   Sasti and Dudhichua PDFs. VINs can be added if the API uses different registration spelling.
4. Install dependencies:

   `python -m pip install -r requirements.txt`

## Test and run

Offline tests:

`python -m unittest discover -s tests -v`

One live report:

`python report_runner.py --site "Sasti Chandrapur" --shift B --report B3 --date 2026-07-20`

Reports are written to `output/pdf`.

## Schedule and stop the browser workflow

Run PowerShell as Administrator:

`.\deployment\Install_Scania_API_Report_Task.ps1`

Only after a live API report has been compared with the portal report, disable the
old task by its exact Windows Task Scheduler name:

`.\deployment\Disable_Old_Browser_Task.ps1 -OldTaskName "Scania Auto Report"`

The stop script refuses to disable the new `Scania API PDF Reports` task.

## Existing browser setup used as reference

`D:\ScaniaAutomation` was inspected read-only. Its two equipment-group IDs,
82 Sasti registrations, 22 Dudhichua registrations, report windows, and saved
API response shape were incorporated into this separate setup. Its browser
tokens were not copied. The existing `Scania Auto Report` task remains enabled
until a direct Data Access report passes live reconciliation.

## Metric source note

Engine time, distance, fuel, idling and coasting come from standardized rFMS
cumulative fields. Power-mode distance is read when Scania supplies a
`performanceModeClass`/`scaniaPerformanceModeClass` labelled `POWER`; otherwise
it is reported as zero, matching most rows in the two supplied legacy PDFs.
