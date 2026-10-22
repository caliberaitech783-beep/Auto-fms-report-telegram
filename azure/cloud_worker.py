"""Persist PDFs and per-chat receipts in this worker's private Blob container."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
import logging
import os
import hashlib
import re
import base64
import zlib
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import threading

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError, HttpResponseError
from azure.storage.blob import BlobServiceClient

from automation.telegram_reports import PROJECTS, deliver, report_cutoff

LOG = logging.getLogger("fleet-cloud")
IST = timezone(timedelta(hours=5, minutes=30))
SOURCE = Path(__file__).resolve().parent
CUTOFFS = ('03:00', '09:00', '11:30', '16:00', '18:00', '23:00')


def download(container, name, destination):
    try:
        data = container.download_blob(name).readall()
    except ResourceNotFoundError:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return True


def prepare(root):
    """Each invocation gets writable, isolated project copies and local secrets."""
    for brand, (folder, _) in PROJECTS.items():
        destination = root / folder
        shutil.copytree(SOURCE / folder, destination)
        prefix = brand.upper()
        credentials = json.loads(os.environ[f"{prefix}_CONFIG_JSON"])
        if not credentials.get("client_id") or not credentials.get("client_secret"):
            raise ValueError(f"Missing {brand} API credentials")
        credentials.pop("site_config", None)
        credentials.pop("shifts_config", None)
        (destination / f"{brand}_config.json").write_text(json.dumps(credentials))
        packed = os.environ.get(f"{prefix}_SITES_ZLIB_B64")
        sites = json.loads(zlib.decompress(base64.b64decode(packed)) if packed
                           else os.environ[f"{prefix}_SITES_JSON"])
        if not sites.get("sites"):
            raise ValueError(f"Missing {brand} site mappings")
        (destination / "site_config.json").write_text(json.dumps(sites))
    cfg = {"bot_token": os.environ["TELEGRAM_BOT_TOKEN"],
            "chats": {brand: [os.environ["TELEGRAM_CHAT_ID"]] for brand in PROJECTS},
            "start_date": os.environ["REPORT_START_DATE"]}
    if os.environ.get('TELEGRAM_ROUTES_JSON'):
        cfg['routes'] = json.loads(os.environ['TELEGRAM_ROUTES_JSON'])
        if not cfg['routes'] or any(not str(chat).strip() for chat in cfg['routes'].values()):
            raise ValueError('Site destinations must be nonempty')
    if os.environ.get('REPORT_SCHEDULE_START'):
        cfg['report_since'] = os.environ['REPORT_SCHEDULE_START']
    return cfg


def run():
    if os.environ.get("REPORTS_ENABLED", "false").lower() != "true":
        LOG.warning("Cloud reporting is disabled pending cutover")
        return
    service = BlobServiceClient.from_connection_string(os.environ["AzureWebJobsStorage"])
    container = service.get_container_client("fleet-report-state")
    try:
        container.create_container()
    except ResourceExistsError:
        pass
    lock = container.get_blob_client('worker.lock')
    try:
        lock.upload_blob(b'', overwrite=False)
    except ResourceExistsError:
        pass
    try:
        lease = lock.acquire_lease(lease_duration=60)
    except HttpResponseError as exc:
        if exc.status_code == 409:
            LOG.info('Another cloud report run holds the lease; skipping overlap')
            return
        raise
    stop = threading.Event()
    lost = threading.Event()
    renewed_at = [time.monotonic()]
    def renew():
        while not stop.wait(20):
            try:
                lease.renew()
                renewed_at[0] = time.monotonic()
            except Exception:
                lost.set()
                return
    def ensure_lease():
        if lost.is_set() or time.monotonic() - renewed_at[0] > 45:
            raise RuntimeError('Storage lease renewal failed; this invocation must stop')
    thread = threading.Thread(target=renew, daemon=True)
    thread.start()
    try:
        _run_locked(container, ensure_lease)
    finally:
        stop.set()
        thread.join(timeout=2)
        try:
            lease.release()
        except Exception:
            LOG.warning('Could not release lease; it will expire automatically')


def _run_locked(container, ensure_lease):
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    with tempfile.TemporaryDirectory(prefix="fleet-reports-") as work:
        root = Path(work)
        cfg = prepare(root)
        progress_file = root / 'progress.json'
        download(container, 'progress.json', progress_file)
        progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}
        now = datetime.now(IST)
        day = now.date().isoformat()
        schedule_start = os.environ.get('REPORT_SCHEDULE_START')
        if schedule_start and progress.get('schedule_start') != schedule_start:
            # New schedule starts prospectively. Keep today's charged runtime,
            # but never replay Testing history into production site groups.
            progress = {'day': day, 'used_seconds': progress.get('used_seconds', 0)
                        if progress.get('day') == day else 0,
                        'completed': {}, 'backlog': {brand: [] for brand in PROJECTS},
                        'schedule_start': schedule_start}
        if progress.get('day') != day:
            backlog = progress.get('backlog', {brand: [] for brand in PROJECTS})
            previous_day = progress.get('day')
            if previous_day:
                cursor = datetime.fromisoformat(previous_day).date()
                while cursor < now.date():
                    queued_day = cursor.isoformat()
                    for brand in PROJECTS:
                        completed = progress.get('completed', {}).get(brand) if queued_day == previous_day else None
                        if completed != CUTOFFS[-1] and queued_day not in backlog.setdefault(brand, []):
                            backlog[brand].append(queued_day)
                    cursor += timedelta(days=1)
            progress = {'day': day, 'used_seconds': 0, 'completed': {}, 'backlog': backlog,
                        'schedule_start': schedule_start}
        progress.setdefault('backlog', {brand: [] for brand in PROJECTS})
        container.upload_blob('progress.json', json.dumps(progress), overwrite=True)
        cutoffs = [value for value in CUTOFFS
                   if value <= now.strftime('%H:%M')]
        cutoff = cutoffs[-1] if cutoffs else None
        database = root / "delivery.sqlite3"
        download(container, "delivery.sqlite3", database)
        acknowledged = set()
        if database.exists():
            with closing(sqlite3.connect(database)) as previous:
                if previous.execute("SELECT name FROM sqlite_master WHERE name='sent'").fetchone():
                    acknowledged = set(previous.execute('SELECT brand,file FROM sent'))
        existing_hashes = {}
        for brand, (folder, _) in PROJECTS.items():
            for blob in container.list_blobs(name_starts_with=f"pdf/{brand}/"):
                filename = Path(blob.name).name
                if filename.endswith(".pdf"):
                    end = report_cutoff(brand, filename)
                    if schedule_start and (end is None or end < datetime.fromisoformat(schedule_start)):
                        continue
                    match = re.search(r'(20\d{2})-?(\d{2})-?(\d{2})', filename)
                    report_day = end.date().isoformat() if end else ('-'.join(match.groups()) if match else None)
                    if (report_day and report_day < day and (brand, filename) in acknowledged
                            and report_day not in progress['backlog'].get(brand, [])):
                        continue
                    local = root / folder / "output/pdf" / filename
                    download(container, blob.name, local)
                    existing_hashes[blob.name] = hashlib.sha256(local.read_bytes()).hexdigest()
        failures = []
        for brand, (folder, script) in PROJECTS.items():
            ensure_lease()
            backlog = progress['backlog'].setdefault(brand, [])
            job_day = min(backlog) if backlog else day
            job_cutoff = CUTOFFS[-1] if backlog else cutoff
            if not job_cutoff or (not backlog and progress['completed'].get(brand) == cutoff):
                continue
            budget = int(os.environ.get('MAX_GENERATION_SECONDS_PER_DAY', '1800'))
            allowance = min(900, max(0, int(budget - progress['used_seconds'])))
            if allowance < 30:
                failures.append(brand + ': daily execution allowance exhausted')
                continue
            progress['used_seconds'] += allowance
            container.upload_blob('progress.json', json.dumps(progress), overwrite=True)
            started = time.monotonic()
            # Prevent unrelated desktop env vars from redirecting configuration.
            env = dict(os.environ)
            env.pop("VOLVO_CONFIG_FILE", None)
            env.pop("VOLVO_SITE_CONFIG_FILE", None)
            env.pop("SCANIA_CONFIG_FILE", None)
            # Azure adds remote-built dependencies to the worker's sys.path;
            # a child interpreter needs the same paths explicitly.
            env['PYTHONPATH'] = os.pathsep.join(sys.path)
            output = ''
            try:
                command = [sys.executable, script, '--as-of', f'{job_day}T{job_cutoff}:00']
                if schedule_start:
                    command += ['--since', schedule_start]
                result = subprocess.run(command, cwd=root / folder,
                                        env=env, timeout=allowance, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                output = getattr(result, 'stdout', '') or ''
                if result.returncode:
                    failures.append(brand)
                else:
                    if backlog:
                        backlog.remove(job_day)
                    else:
                        progress['completed'][brand] = cutoff
            except subprocess.TimeoutExpired as exc:
                output = exc.stdout or ''
                if isinstance(output, bytes):
                    output = output.decode('utf-8', errors='replace')
                output += '\nGeneration timed out; will retry within the daily allowance.'
                failures.append(brand)
            except OSError as exc:
                output = str(exc)
                failures.append(brand)
            finally:
                ensure_lease()
                progress['used_seconds'] -= max(0, allowance - (time.monotonic() - started))
                container.upload_blob('progress.json', json.dumps(progress), overwrite=True)
                for prefix in ('SCANIA', 'VOLVO'):
                    secrets = json.loads(os.environ[prefix + '_CONFIG_JSON'])
                    for key in ('client_id', 'client_secret'):
                        value = secrets.get(key)
                        if value:
                            output = output.replace(value, '[redacted]')
                output = output.replace(os.environ['TELEGRAM_BOT_TOKEN'], '[redacted]')
                container.upload_blob(f'logs/{brand}-latest.txt', output[-64000:], overwrite=True)
            # Store every generated PDF before sending; retry after restarts.
            for pdf in (root / folder / "output/pdf").glob("*.pdf"):
                name = f"pdf/{brand}/{pdf.name}"
                data = pdf.read_bytes()
                if existing_hashes.get(name) != hashlib.sha256(data).hexdigest():
                    container.upload_blob(name, data, overwrite=True)
        with closing(sqlite3.connect(database)) as db:
            def checkpoint():
                ensure_lease()
                container.upload_blob("delivery.sqlite3", database.read_bytes(), overwrite=True)
            send_failures = deliver(cfg, db, root, checkpoint=checkpoint, before_send=ensure_lease)
            checkpoint()
            summary = {"updated_at": datetime.now(IST).isoformat(),
                       "generation_failures": failures, "delivery_failures": send_failures,
                       "generation_seconds_today": progress['used_seconds'],
                       "queued_days": progress['backlog'],
                       "schedule_cutoffs_ist": CUTOFFS, "schedule_start": schedule_start,
                       "site_routes": cfg.get('routes', {}),
                       "receipts": dict(db.execute("SELECT brand,COUNT(*) FROM sent GROUP BY brand"))}
            container.upload_blob("status.json", json.dumps(summary), overwrite=True)
        LOG.info("Fleet cloud run complete: %s", summary)
        if failures or send_failures:
            raise RuntimeError("Some fleet reports failed; pending work will retry next timer run")
