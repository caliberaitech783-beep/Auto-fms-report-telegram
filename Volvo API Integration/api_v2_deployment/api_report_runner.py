"""API-only scheduled pipeline: Volvo APIs -> PDFs -> optional WhatsApp API."""
from __future__ import annotations

import json
import logging
import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import winsound
except ImportError:  # pragma: no cover - this automation runs on Windows
    winsound = None

import volvo_api_v2 as volvo
from api_pdf_report import build_pdf
from whatsapp_sender import load_config, send_pdf


BASE = Path(__file__).resolve().parent
OUTBOX = BASE / "output" / "pdf"
LOG_DIR = BASE / "logs"
SHIFTS_FILE = BASE / "shifts.json"
DRIVER_MAP_FILE = BASE / "driver_vehicle_map.json"
QUEUE_FILE = OUTBOX / "whatsapp_queue.jsonl"

LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_DIR / "api_reports.log", encoding="utf-8")],
)
log = logging.getLogger("api-reports")


def _play_signal(kind: str) -> None:
    """Audible completion status for unattended scheduled runs."""
    if winsound is None:
        return
    if kind == "success":
        pattern = [(1800, 160), (1800, 160)]
        gap_s = 0.10
    elif kind == "failure":
        pattern = [(450, 450)] * 4
        gap_s = 0.15
    else:
        return
    for index, (frequency, duration_ms) in enumerate(pattern):
        winsound.Beep(frequency, duration_ms)
        if index + 1 < len(pattern):
            time.sleep(gap_s)


def _active_window(site: str, now: datetime, shifts: dict):
    for shift, (start_s, stop_s) in shifts["sites"][site].items():
        sh, sm = map(int, start_s.split(":"))
        eh, em = map(int, stop_s.split(":"))
        start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
        end = now.replace(hour=eh, minute=em, second=59, microsecond=0)
        if start <= end:
            if start <= now <= end:
                return shift, start
        else:
            if now.time() >= start.time():
                return shift, start
            if now.time() <= end.time():
                return shift, start - timedelta(days=1)
    return None


def _write_queue(entry: dict):
    OUTBOX.mkdir(parents=True, exist_ok=True)
    with QUEUE_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def main(as_of: str | None = None) -> int | None:
    # Convert the machine-local wall clock through the integration's parser so
    # all report boundaries are timezone-aware IST values.
    now = (volvo.parse_when(as_of) if as_of else
           volvo.parse_when(datetime.now().replace(second=0, microsecond=0).isoformat()))
    shifts = json.loads(SHIFTS_FILE.read_text(encoding="utf-8"))
    site_cfg = volvo.load_site_config()
    driver_cache = ({"by_vehicle_id": {}, "by_vehicle_name": {}}
                    if not DRIVER_MAP_FILE.exists()
                    else json.loads(DRIVER_MAP_FILE.read_text(encoding="utf-8")))
    sites = list((site_cfg.get("sites") or {}).keys())
    windows = {site: _active_window(site, now, shifts) for site in sites}
    windows = {site: win for site, win in windows.items() if win}
    if not windows:
        log.warning("No active site shifts at %s", now)
        return None  # No scheduled report due: successful exit, no sound.

    earliest = min(start for _, start in windows.values())
    client_id, secret = volvo.load_credentials()
    auth = volvo.auth_header(client_id, secret)
    display_names = volvo.fetch_vehicle_display_names(auth)
    fetch_start = earliest - timedelta(minutes=volvo.PRE_WINDOW_MIN)
    log.info("Fetching fleet once for %s through %s", fetch_start, now)
    snapshots = volvo.fetch_statuses(volvo._utc_z(fetch_start), volvo._utc_z(now), auth)
    by_vin = volvo.group_by_vin(snapshots)

    wa_cfg = load_config(BASE)
    failures = []

    for site, (shift, start) in windows.items():
        rows = []
        wanted = volvo.vins_for_site(site, site_cfg)
        vehicle_by_vin = {v.get("vin"): v for v in volvo._site_vehicles(site, site_cfg)}
        for vin in sorted(wanted):
            meta = volvo.meta_for_vin(vin, site, site_cfg, display_names)
            vehicle_cfg = vehicle_by_vin.get(vin) or {}
            cached_driver = ((driver_cache.get("by_vehicle_id") or {}).get(vehicle_cfg.get("id"))
                             or (driver_cache.get("by_vehicle_name") or {}).get(vehicle_cfg.get("name")))
            if cached_driver:
                meta["driver"] = cached_driver
            diff = volvo.window_diffs(by_vin.get(vin, []), start, now)
            if diff is None:
                row = {column: "" for column in volvo.COLUMNS}
                row.update({"Vehicle": meta.get("registration") or vin,
                            "Driver": meta.get("driver", "Unknown")})
            else:
                row = volvo.compute_row(vin, diff, meta)
            rows.append(row)

        try:
            result = build_pdf(rows, site, shift, start, now, OUTBOX)
            if not result:
                log.info("%s Shift %s: no vehicles met the 30-minute minimum; skipped",
                         site, shift)
                continue
            pdf = Path(result["pdf"] if isinstance(result, dict) else result)
            entry = {"created_at": datetime.now().isoformat(), "site": site,
                     "shift": shift, "pdf": str(pdf), "status": "pending"}
            if wa_cfg:
                sent = send_pdf(pdf, site, f"{site} Shift {shift} report - {now:%d-%b-%Y %H:%M}", wa_cfg)
                entry.update({"status": "sent", "deliveries": sent})
            _write_queue(entry)
            log.info("%s Shift %s -> %s (%s)", site, shift, pdf.name, entry["status"])
        except Exception as exc:
            failures.append(site)
            log.exception("%s failed: %s", site, exc)

    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate browser-free Volvo API PDFs.")
    parser.add_argument("--as-of", help="Backfill cutoff in site-local IST, e.g. 2026-07-24T11:45:00")
    parser.add_argument("--test-sound", choices=("success", "failure"),
                        help="Play a signal only; do not call APIs or generate reports")
    args = parser.parse_args()
    if args.test_sound:
        _play_signal(args.test_sound)
        raise SystemExit(0)

    try:
        result = main(args.as_of)
    except (Exception, KeyboardInterrupt):
        log.exception("Scheduled report run failed")
        _play_signal("failure")
        raise SystemExit(1)

    if result is None:
        raise SystemExit(0)
    _play_signal("success" if result == 0 else "failure")
    raise SystemExit(result)
