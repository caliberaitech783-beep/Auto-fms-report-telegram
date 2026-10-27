"""API-only scheduled pipeline: Volvo APIs -> PDFs -> optional WhatsApp API."""
from __future__ import annotations

import json
import logging
import argparse
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    import winsound
except ImportError:  # pragma: no cover - this automation runs on Windows
    winsound = None

import volvo_api_v2 as volvo
from asset_group_sync import sync_asset_groups
from fleet_cache import FleetCache, CollectionPending
from api_pdf_report import build_pdf, report_filename
from whatsapp_sender import load_config, send_pdf


BASE = Path(__file__).resolve().parent
OUTBOX = BASE / "output" / "pdf"
LOG_DIR = BASE / "logs"
SHIFTS_FILE = BASE / "shifts.json"
QUEUE_FILE = OUTBOX / "whatsapp_queue.jsonl"
SCHEDULED_REPORT_TIMES = ("03:00", "09:00", "11:30", "16:00", "18:00", "23:00")

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


def scheduled_cutoffs(now: datetime,
                      schedule: tuple[str, ...] = SCHEDULED_REPORT_TIMES) -> list[datetime]:
    """Today's scheduled cutoffs that have passed, oldest first.

    Only ``now``'s calendar date is considered. This intentionally never
    catches up a previous date and never returns a future cutoff.
    """
    cutoffs = []
    for value in schedule:
        hour, minute = map(int, value.split(":"))
        cutoff = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cutoff <= now:
            cutoffs.append(cutoff)
    return cutoffs


def plan_missing_reports(now: datetime, sites: list[str], shifts: dict,
                         outbox: Path = OUTBOX, since: datetime | None = None) -> list[dict]:
    """Plan today's due site reports, excluding exact outputs already present."""
    jobs = []
    for cutoff in scheduled_cutoffs(now):
        if since and cutoff < since:
            continue
        for site in sites:
            window = _active_window(site, cutoff, shifts)
            if not window:
                continue
            shift, start = window
            expected = outbox / report_filename(site, shift, start, cutoff)
            if expected.exists() or Path(str(expected) + '.empty.json').exists():
                log.info("Already generated; skipping %s", expected.name)
                continue
            jobs.append({"site": site, "shift": shift, "start": start,
                         "cutoff": cutoff, "expected": expected})
    return jobs


def _write_queue(entry: dict):
    OUTBOX.mkdir(parents=True, exist_ok=True)
    with QUEUE_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=True) + "\n")


def configured_sites(site_cfg: dict) -> list[str]:
    sites = list((site_cfg.get('sites') or {}).keys())
    if os.environ.get('TELEGRAM_ROUTES_JSON'):
        routes = json.loads(os.environ['TELEGRAM_ROUTES_JSON'])
        sites = [site for site in sites if any(key.casefold() in site.casefold() for key in routes)]
    return sites


def main(as_of: str | None = None, since: str | None = None, collect_only: bool = False) -> int | None:
    # Convert the machine-local wall clock through the integration's parser so
    # all report boundaries are timezone-aware IST values.
    now = (volvo.parse_when(as_of) if as_of else
           volvo.parse_when(datetime.now().replace(second=0, microsecond=0).isoformat()))
    shifts = json.loads(SHIFTS_FILE.read_text(encoding="utf-8"))
    try:
        site_cfg, _ = sync_asset_groups(volvo.SITE_CONFIG_FILE, shifts, BASE, log)
    except Exception as exc:
        log.error("Asset-group synchronization failed; using last valid mapping: %s", exc)
        site_cfg = volvo.load_site_config()
    # Cloud destinations are explicit. Unmapped/Unknown sites must not keep
    # reopening old windows and exhaust the budget for real site reports.
    sites = configured_sites(site_cfg)
    jobs = plan_missing_reports(now, sites, shifts, OUTBOX,
                                since=volvo.parse_when(since) if since else None)
    incremental = os.environ.get("VOLVO_INCREMENTAL_CACHE", "false").lower() == "true"
    if not jobs and not (incremental and collect_only):
        log.info("No scheduled report due or all due outputs already exist at %s", now)
        return None  # No work due: successful exit, no sound.

    if collect_only:
        jobs = []
    earliest = min((job["start"] for job in jobs), default=now.replace(hour=0, minute=0) - timedelta(hours=6))
    latest = max((job["cutoff"] for job in jobs), default=now)
    auth = volvo.auth_header(*volvo.load_credentials())
    fetch_start = earliest - timedelta(minutes=volvo.PRE_WINDOW_MIN)
    if incremental:
        cache = FleetCache(BASE / "state/fleet.sqlite3")
        try:
            if collect_only and cache.get('coverage'):
                fetch_start = volvo.parse_when(cache.get('coverage')['start'])
            site_cfg = cache.get('site_config', site_cfg)
            # Metadata discovery is independent of status pagination. Preserve the
            # last valid names/mappings if Volvo's inventory endpoint is down.
            try:
                site_cfg, display_names, driver_directory = cache.metadata(auth, site_cfg, sites, now,
                                                                         force_inventory=not collect_only)
                cache.put('metadata_error', None)
            except Exception as exc:
                cache.put('metadata_error', type(exc).__name__)
                display_names = {v['vin']: v.get('customerVehicleName', '') for v in cache.get('inventory', [])}
                driver_directory = cache.get('drivers', {})
                log.warning('Vehicle inventory refresh failed; retaining saved mapping: %s', type(exc).__name__)
            try:
                cache.collect(fetch_start, latest, auth,
                              seconds=float(os.environ.get('VOLVO_COLLECTION_SLICE_SECONDS', '75')))
            except CollectionPending:
                log.info('Incremental collection checkpoint saved; next timer resumes it')
                return 75
            cache.put('last_collected', latest.isoformat())
            cache.put('health', {'coverage': cache.get('coverage'),
                                'pending_vehicles': len(cache.get('unassigned_vehicles', [])),
                                'metadata_error': cache.get('metadata_error')})
            if collect_only:
                return None
            snapshots = cache.snapshots(fetch_start, latest)
        finally:
            cache.close()
    else:
        display_names = volvo.fetch_vehicle_display_names(auth)
        driver_directory = volvo.fetch_driver_directory(auth)
        log.info("Recovering %d missing report(s); fetching fleet once for %s through %s",
                 len(jobs), fetch_start, latest)
        snapshots = volvo.fetch_statuses(volvo._utc_z(fetch_start), volvo._utc_z(latest), auth)
    by_vin = volvo.group_by_vin(snapshots)

    wa_cfg = load_config(BASE)
    failures = []

    for job in jobs:
        site, shift = job["site"], job["shift"]
        start, cutoff = job["start"], job["cutoff"]
        expected = job["expected"]
        # Re-check immediately before generating in case another task instance
        # completed this exact output after the recovery plan was created.
        if expected.exists():
            log.info("Already generated during this run; skipping %s", expected.name)
            continue
        rows = []
        wanted = volvo.vins_for_site(site, site_cfg)
        for vin in sorted(wanted):
            meta = volvo.meta_for_vin(vin, site, site_cfg, display_names)
            meta["driver"] = volvo.driver_name_for_window(
                by_vin.get(vin, []), start, cutoff, driver_directory)
            diff = volvo.window_diffs(by_vin.get(vin, []), start, cutoff)
            if diff is None:
                row = {column: "" for column in volvo.COLUMNS}
                row.update({"Vehicle": meta.get("registration") or vin,
                            "Driver": meta.get("driver", "Unknown")})
            else:
                row = volvo.compute_row(vin, diff, meta)
            rows.append(row)

        try:
            result = build_pdf(rows, site, shift, start, cutoff, OUTBOX)
            if not result:
                expected.parent.mkdir(parents=True, exist_ok=True)
                Path(str(expected) + '.empty.json').write_text(json.dumps({
                    'site': site, 'shift': shift, 'start': start.isoformat(),
                    'cutoff': cutoff.isoformat(), 'reason': 'no qualifying vehicles'}))
                log.info("%s Shift %s at %s: no vehicles met the 30-minute and "
                         "15-km minimums; skipped", site, shift, cutoff)
                continue
            pdf = Path(result["pdf"] if isinstance(result, dict) else result)
            entry = {"created_at": datetime.now().isoformat(), "site": site,
                     "shift": shift, "pdf": str(pdf), "status": "pending"}
            if wa_cfg:
                sent = send_pdf(pdf, site,
                                f"{site} Shift {shift} report - {cutoff:%d-%b-%Y %H:%M}",
                                wa_cfg)
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
    parser.add_argument("--collect-only", action="store_true", help="Save new fleet data without generating PDFs")
    parser.add_argument("--since", help="Exclude cutoffs before this IST activation timestamp.")
    parser.add_argument("--test-sound", choices=("success", "failure"),
                        help="Play a signal only; do not call APIs or generate reports")
    args = parser.parse_args()
    if args.test_sound:
        _play_signal(args.test_sound)
        raise SystemExit(0)

    try:
        result = main(args.as_of, args.since, args.collect_only)
    except (Exception, KeyboardInterrupt):
        log.exception("Scheduled report run failed")
        _play_signal("failure")
        raise SystemExit(1)

    if result is None:
        raise SystemExit(0)
    if result != 75:
        _play_signal("success" if result == 0 else "failure")
    raise SystemExit(result)
