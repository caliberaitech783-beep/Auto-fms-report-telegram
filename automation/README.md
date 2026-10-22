# Automatic Telegram reports

`telegram_reports.py` runs Scania's scheduled runner and Volvo's recovery runner,
then uploads their PDFs through Telegram's `sendDocument` API. Run this wrapper
to get Telegram delivery; the original standalone runners still generate PDFs only.
The report cutoffs remain 10:30, 11:45, 15:00, 17:00 and 18:45 IST.

## Local setup

1. Create a bot with Telegram's official `@BotFather`. Add it to the intended
   group and allow it to send documents (or start the bot in your private chat).
2. Copy `telegram_config.example.json` in the repository root to
   `telegram_config.json`. Put the token in this ignored local file, or set
   `TELEGRAM_BOT_TOKEN` in the environment of the account running the task.
   Enter the numeric chat IDs in `chats.scania` and `chats.volvo`. Both may use
   the same chat. Multiple chat IDs per brand are supported. Never commit tokens.
3. Configure Scania credentials in `Scania API Integration/scania_config.json`.
   Set both your client ID and secret. Configure Volvo credentials and site
   mappings using `VOLVO_CONFIG_FILE` and `VOLVO_SITE_CONFIG_FILE`, or place
   `volvo_config.json` and `site_config.json` in
   `Volvo API Integration/api_v2_recovery`. The import did not contain Volvo's
   live credential/site configuration. Existing local files can be used.
4. Install both projects' requirements with the Python interpreter that will
   run the task. This Telegram wrapper itself uses only Python's standard library.
5. On the report computer, set the Windows timezone to India Standard Time.

From the repository root, using your chosen Python executable:

```powershell
python automation/telegram_reports.py --check
python automation/telegram_reports.py
```

`--check` checks the bot and chat access without sending documents. The second
command generates due reports and sends them. Telegram document permissions are
only fully verified by a successful upload. To retry delivery without fleet API
calls, use `--delivery-only`.

## Automatic Windows task

```powershell
.\automation\Install-TelegramReports.ps1 -Python 'C:\path\to\python.exe'
```

The installer verifies Telegram configuration first, then installs **Fleet Reports
to Telegram** to run every five minutes and at login. Reports are generated after
their configured cutoffs; a delivery can therefore occur up to five minutes later,
plus API/report processing time. The task runs only while the configured Windows
user is logged in and the computer is awake with internet access. Hosting on an
always-on Windows machine is required for unattended delivery through downtime.
Existing generation tasks are not changed; avoid running duplicate generation
tasks against the same directories when enabling this combined task.

If report generation is already scheduled, use `-DeliveryOnly` when installing
the task. It will only upload pending PDFs. Set local `project_dirs.scania` and
`project_dirs.volvo` to the existing integration directories; each is expected
to contain `output/pdf`. Set `settle_seconds` to 30 to allow new PDFs to finish
writing before uploading them. Keep the existing fleet generation tasks enabled
when using this mode.

## Delivery state and retries

Successful uploads are recorded separately for each PDF content hash and chat in
ignored `automation/state/delivery.sqlite3`. Failed sends are retried next run,
including when the PDF was generated on an earlier run. A failed Scania generation
does not prevent Volvo generation or delivery of PDFs already present.
The first delivery run saves its IST date as the earliest eligible file creation/
modification date. Set `start_date` explicitly as `YYYY-MM-DD` to include an older
backlog. With an empty setting, files modified before activation are excluded.
Keep the state database when moving the installation to avoid repeat deliveries.
If Telegram accepts a document but the response is lost, or the process stops
before saving its receipt, a later retry can deliver it twice.

Offline sender tests:

```powershell
python -m unittest discover -s automation -p 'test_*.py' -v
```
