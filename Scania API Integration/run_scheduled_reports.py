"""Generate due reports, including recently missed reports after laptop downtime."""
from __future__ import annotations

import argparse
import json
import logging
try:
    import winsound
except ImportError:
    winsound = None
from datetime import date, datetime, time, timedelta
from pathlib import Path
from time import sleep

from report_runner import generate

BASE = Path(__file__).resolve().parent
LOGS = BASE / "logs"
LOGS.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOGS / "scheduled.log", encoding="utf-8")],
)
log = logging.getLogger("scania-scheduled")
SCHEDULED_REPORTS = ("A1", "A2", "B1", "B2", "B3")
DEFAULT_LOOKBACK_DAYS = 0
REPORT_ATTEMPTS = 2
REPORT_RETRY_DELAY_SECONDS = 30


def report_due(report_day: date, stop_s: str, now: datetime) -> bool:
    stop_h, stop_m = map(int, stop_s.split(":"))
    scheduled = datetime.combine(report_day, time(stop_h, stop_m))
    return scheduled <= now


def expected_path(site: str, report: str, report_day: date, stop_s: str) -> Path:
    stop_h, stop_m = map(int, stop_s.split(":"))
    return BASE / "output" / "pdf" / (
        f"{site.replace(' ', '_')}_{report}_{report_day:%Y%m%d}_{stop_h:02d}{stop_m:02d}.pdf"
    )


def due_missing_reports(now: datetime, shifts: dict, sites: dict,
                        lookback_days: int = DEFAULT_LOOKBACK_DAYS):
    first_day = now.date() - timedelta(days=max(0, lookback_days))
    for day_offset in range((now.date() - first_day).days + 1):
        report_day = first_day + timedelta(days=day_offset)
        for site, site_shifts in shifts["sites"].items():
            if not (sites.get("sites", {}).get(site, {}).get("api_enabled", False)):
                continue
            for report in SCHEDULED_REPORTS:
                if report not in site_shifts["reports"]:
                    continue
                _, stop_s = site_shifts["reports"][report]
                path = expected_path(site, report, report_day, stop_s)
                if report_due(report_day, stop_s, now) and not path.exists():
                    yield site, report, report_day


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help="Generate missing scheduled reports from this many previous days (default: 0/current date only).",
    )
    args = parser.parse_args(argv)
    now = datetime.now()
    shifts = json.loads((BASE / "shifts.json").read_text(encoding="utf-8"))
    sites = json.loads((BASE / "site_config.json").read_text(encoding="utf-8"))
    generated = 0
    pending = list(due_missing_reports(
        now, shifts, sites, lookback_days=args.lookback_days
    ))
    for attempt in range(1, REPORT_ATTEMPTS + 1):
        failed = []
        for site, report, report_day in pending:
            try:
                generate(site, report[0], report_day.isoformat(), report=report)
                generated += 1
            except Exception:
                failed.append((site, report, report_day))
                log.exception(
                    "Failed (attempt %d/%d): %s Report %s for %s",
                    attempt, REPORT_ATTEMPTS, site, report, report_day,
                )
        pending = failed
        if not pending or attempt == REPORT_ATTEMPTS:
            break
        log.warning("Retrying %d failed report(s) in %d seconds.",
                    len(pending), REPORT_RETRY_DELAY_SECONDS)
        sleep(REPORT_RETRY_DELAY_SECONDS)
    failures = len(pending)
    if failures and winsound is not None:
        # Four longer low tones mean at least one expected report failed.
        try:
            for _ in range(4):
                winsound.Beep(450, 500)
        except RuntimeError:
            log.warning("Report generation failed, but the Windows beep device was unavailable.")
    elif generated and winsound is not None:
        # Two short high tones mean all due reports were created successfully.
        try:
            winsound.Beep(1000, 250)
            winsound.Beep(1300, 350)
        except RuntimeError:
            log.warning("Reports generated, but the Windows beep device was unavailable.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
