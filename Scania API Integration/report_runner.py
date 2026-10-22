"""Generate browser-free Scania shift reports for configured sites."""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from scania_api import ScaniaClient, compute_row, group_by_vin, load_config, match_site_vehicles, parse_when, window_diffs
from scania_pdf_report import build_pdf

BASE = Path(__file__).resolve().parent
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scania-report")


def shift_window(site: str, shift: str, day: str, shifts: dict):
    start_s, stop_s = shifts["sites"][site][shift]
    start = parse_when(f"{day}T{start_s}:00")
    stop = parse_when(f"{day}T{stop_s}:59")
    if stop <= start:
        stop += timedelta(days=1)
    return start, stop


def report_window(site: str, report: str, day: str, shifts: dict):
    start_s, stop_s = shifts["sites"][site]["reports"][report]
    start = parse_when(f"{day}T{start_s}:00")
    stop = parse_when(f"{day}T{stop_s}:00")
    if stop <= start:
        stop += timedelta(days=1)
    return start, stop


def generate(site: str, shift: str, day: str, output_dir: Path | None = None,
             report: str | None = None) -> Path:
    config = load_config()
    sites = json.loads(Path(config.get("site_config", BASE / "site_config.json")).read_text(encoding="utf-8"))
    shifts = json.loads(Path(config.get("shifts_config", BASE / "shifts.json")).read_text(encoding="utf-8"))
    start, stop = (
        report_window(site, report, day, shifts)
        if report else shift_window(site, shift, day, shifts)
    )
    client = ScaniaClient(
        config["client_id"], config["client_secret"],
        auth_base=config.get("auth_base", "https://dataaccess.scania.com/auth"),
        api_base=config.get("api_base", "https://dataaccess.scania.com/rfms4"),
    )
    vehicles = match_site_vehicles(client.vehicles(), sites, site)
    if not vehicles:
        raise RuntimeError(f"No API vehicles matched site {site}; update site_config.json.")
    # Fetch a generous pre-window baseline because parked vehicles may not emit
    # a TIMER record close to shift start. A shorter post-window allowance is
    # sufficient for the end boundary and keeps scheduled fetches compact.
    snapshots = client.vehicle_statuses(start - timedelta(hours=12), stop + timedelta(minutes=90))
    by_vin = group_by_vin(snapshots)
    rows = []
    for vin, vehicle in vehicles.items():
        diff = window_diffs(by_vin.get(vin, []), start, stop)
        if diff:
            rows.append(compute_row(vin, diff, vehicle))
        else:
            log.warning("No usable status window for %s (%s)", vehicle.get("name"), vin)
    path = build_pdf(rows, site, report or shift, start, stop, output_dir)
    log.info("Created %s", path)
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--site", required=True)
    parser.add_argument("--shift", required=True, choices=("A", "B", "C"))
    parser.add_argument("--report", choices=("A1", "A2", "B1", "B2", "B3", "C1", "C2", "C3"))
    parser.add_argument("--date", default=datetime.now().date().isoformat())
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    generate(args.site, args.shift, args.date, args.output_dir, args.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
