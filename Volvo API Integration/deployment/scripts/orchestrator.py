"""
orchestrator.py  —  run this every 2 hours (via Task Scheduler).
For each site: work out the current cumulative shift window, fetch that site's
12-column data, and write a highlighted PDF into the reports folder.

    python orchestrator.py            # normal run, all sites
    python orchestrator.py Sasti      # just one site (testing)
"""
import os, sys, json, time, logging, subprocess, datetime as dt
from paths import CONFIG, PDF_OUTBOX, LOGS, HERE
import volvo_api_fetch as vf
import shift_window as sw
import report_writer as rw
import pdf_report as pr
import driver_names as dn

os.makedirs(LOGS, exist_ok=True)
logging.basicConfig(level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler(sys.stdout),
              logging.FileHandler(os.path.join(LOGS, "orchestrator.log"), encoding="utf-8")])
log = logging.getLogger("orch")

def _beep():
    """Audible alert after reports are generated (through the speakers)."""
    # Method 1: play a real Windows sound via the sound card (works on laptops)
    try:
        import winsound
        for _ in range(4):
            winsound.PlaySound("SystemExclamation", winsound.SND_ALIAS)
            time.sleep(0.35)
        return
    except Exception:
        pass
    # Method 2: legacy PC-speaker beep (may be silent on laptops)
    try:
        import winsound
        for _ in range(3):
            winsound.Beep(1000, 400); time.sleep(0.15)
        return
    except Exception:
        pass
    # Method 3: terminal bell
    try: print("\a\a\a", end="", flush=True)
    except Exception: pass

TOKEN_MIN_REMAINING = 20 * 60 * 1000   # refresh if <20 min left (ms)

def ensure_token():
    """Return a valid token, refreshing via get_token.py if needed."""
    path = os.path.join(CONFIG, "token.json")
    def remaining():
        try:
            with open(path) as f: t = json.load(f)
            return (t.get("expiresAt", 0) - time.time()*1000)
        except Exception:
            return -1
    # Always grab the CURRENT live token from the open debug browser (like the
    # Scania grabber), so any token change on the portal is picked up every run.
    log.info("Grabbing live token from the open browser...")
    subprocess.run([sys.executable, os.path.join(HERE, "get_token.py")],
                   check=False, timeout=180)
    if remaining() < 0:
        raise RuntimeError("No valid token. Make sure Start_Volvo_Browser.bat is "
                           "open and logged in to Volvo Connect.")
    return vf.load_token()

def load_sites():
    with open(os.path.join(CONFIG, "site_config.json")) as f:
        cfg = json.load(f)
    return cfg["sites"], cfg.get("fleetId", "0196596D918F4C54A60EFCB9F83738C5")

def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    now = dt.datetime.now()
    thr = rw._thr()
    token = ensure_token()
    sites, fleet_id = load_sites()
    shifts = sw._load_shifts()
    try:
        driver_map = dn.fetch_driver_map(token)
        log.info(f"Loaded {len(driver_map)} driver names")
    except Exception as e:
        log.warning(f"Driver names unavailable ({e}); showing Unknown"); driver_map = {}

    targets = [only] if only else list(sites.keys())
    ok, fail = [], []
    for site in targets:
        try:
            vehicles = sites[site]["vehicles"] if "vehicles" in sites[site] else sites[site]
            ids = [v["id"] for v in vehicles]
            id_to_name = {v["id"]: v["name"] for v in vehicles}
            win = sw.active_window(site, now, shifts)
            if not win:
                log.warning(f"{site}: no active shift, skipping"); continue
            s_iso, e_iso = vf.ist_window(win["start"], now)
            data = vf.fetch_raw(token, ids, s_iso, e_iso, fleet_id)
            rows = vf.map_rows(data, id_to_name, driver_map=driver_map,
                               economy_field=thr.get("economy_field","consumptionPercentage"),
                               coasting_field=thr.get("coasting_field","distancePercentage"))
            res = pr.build_pdf(rows, site, win["shift"], win["start"], now,
                               PDF_OUTBOX)
            if res:
                log.info(f"{site} Shift {win['shift']} {win['window_text']}: "
                         f"{res['vehicles']} vehicles ({res['idling']} idling) -> {os.path.basename(res['pdf'])}")
                ok.append(site)
            else:
                log.warning(f"{site}: no vehicles above min engine time"); ok.append(site)
        except Exception as e:
            log.error(f"{site}: FAILED - {e}"); fail.append(site)
    log.info(f"Done. ok={ok} fail={fail}")
    if ok:
        _beep()
    return 0 if not fail else 1

if __name__ == "__main__":
    sys.exit(main())
