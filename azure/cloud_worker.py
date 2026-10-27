"""Persist PDFs and per-chat receipts in this worker's private Blob container."""
from datetime import datetime, timedelta, timezone
from contextlib import closing
import json
import logging
import os
import hashlib
import gzip
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

from automation.telegram_reports import PROJECTS, deliver, report_cutoff, api

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
        start = datetime.fromisoformat(os.environ['REPORT_SCHEDULE_START'])
        cfg['report_since'] = (start.astimezone(IST) if start.tzinfo
                               else start.replace(tzinfo=IST)).isoformat()
    return cfg


def generation_allowance(progress, brand, now, background=False, backlog=False):
    """Bound retries and protect runtime for every later scheduled batch."""
    budget = int(os.environ.get('MAX_GENERATION_SECONDS_PER_DAY', '1800'))
    available = max(0, budget - progress['used_seconds'])
    if os.environ.get('VOLVO_INCREMENTAL_CACHE', 'false').lower() != 'true':
        return min(900, int(available))
    future = sum(value > now.strftime('%H:%M') for value in CUTOFFS)
    reserve = future * min(240, budget / 12)
    if background or backlog:
        reserve += min(240, budget / 12)
    available = max(0, available - reserve)
    if background:
        available = min(available, max(0, 1800 - progress.get('collection_seconds', 0)))
    return int(min(90 if background else (180 if brand == 'scania' else 300), available))


def collection_due(progress, now):
    previous = progress.get('collection_attempt')
    if previous and (now - datetime.fromisoformat(previous)).total_seconds() < 600:
        return False
    # Half-hourly collection plus a five-minute prefetch before each cutoff.
    minute = now.hour * 60 + now.minute
    return now.minute % 30 < 5 or any(0 < (int(c[:2])*60 + int(c[3:])) - minute <= 5 for c in CUTOFFS)


def save_cache(container, cache_file):
    # A subprocess may have been killed mid-transaction. SQLite's backup API
    # recovers its local rollback journal before publishing a coherent snapshot.
    target = cache_file.with_suffix('.backup.sqlite3')
    with closing(sqlite3.connect(cache_file)) as source, closing(sqlite3.connect(target)) as backup:
        source.backup(backup)
    container.upload_blob('cache/volvo/fleet.sqlite3.gz', gzip.compress(target.read_bytes()), overwrite=True)


def notify_delays(cfg, progress, now, failures, ensure_lease, container):
    """One operational notice per affected brand/cutoff/group, after ten minutes."""
    if not failures:
        return
    due = [c for c in CUTOFFS if c <= now.strftime('%H:%M')]
    if not due:
        return
    cutoff = due[-1]
    boundary = now.replace(hour=int(cutoff[:2]), minute=int(cutoff[3:]), second=0, microsecond=0)
    if (now - boundary).total_seconds() < 600:
        return
    notices = progress.setdefault('delay_notices', [])
    for brand in PROJECTS:
        if progress['completed'].get(brand) == cutoff or not any(f.startswith(brand) for f in failures):
            continue
        routes = cfg.get('routes', {})
        chats = {str(chat) for site, chat in routes.items()
                 if brand == 'volvo' or site.casefold() in ('sasti', 'dudhichua')}
        for chat in chats:
            key = f'{progress["day"]}:{brand}:{cutoff}:{chat}'
            if key in notices:
                continue
            ensure_lease()
            try:
                api(cfg['bot_token'], 'sendMessage', {'chat_id': chat, 'text':
                    f'{brand.title()} {cutoff} IST report delayed on {progress["day"]}. '
                    'Automatic recovery is pending within the daily runtime limit; '
                    'later report times keep reserved runtime.'})
                notices.append(key)
                container.upload_blob('progress.json', json.dumps(progress), overwrite=True)
            except Exception:
                LOG.warning('Could not deliver report-delay notice; will retry')


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
        schedule_start = cfg.get('report_since')
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
        incremental = os.environ.get('VOLVO_INCREMENTAL_CACHE', 'false').lower() == 'true'
        cache_file = root / PROJECTS['volvo'][0] / 'state/fleet.sqlite3'
        cache_archive = root / 'fleet.sqlite3.gz'
        if incremental and download(container, 'cache/volvo/fleet.sqlite3.gz', cache_archive):
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(gzip.decompress(cache_archive.read_bytes()))
        existing_hashes = {}
        for brand, (folder, _) in PROJECTS.items():
            for blob in container.list_blobs(name_starts_with=f"pdf/{brand}/"):
                filename = Path(blob.name).name
                if filename.endswith((".pdf", ".pdf.empty.json")):
                    end = report_cutoff(brand, filename.removesuffix('.empty.json'))
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
            current_due = cutoff and progress['completed'].get(brand) != cutoff
            # Current reports take priority; durable older work uses spare time.
            use_backlog = bool(backlog) and (not incremental or not current_due)
            job_day = min(backlog) if use_backlog else day
            job_cutoff = CUTOFFS[-1] if use_backlog else cutoff
            background = (incremental and brand == 'volvo' and not current_due
                          and not use_backlog and collection_due(progress, now))
            if not background and (not job_cutoff or (not use_backlog and not current_due)):
                continue
            if incremental and not background:
                slot = f'{brand}:{job_day}:{job_cutoff}'
                slot_remaining = (240 if brand == 'scania' else 480) - progress.setdefault('slot_seconds', {}).get(slot, 0)
                if slot_remaining < 30:
                    failures.append(brand + ': batch retry allowance reached; later cutoffs retain reserved runtime')
                    continue
            allowance = generation_allowance(progress, brand, now, background, use_backlog)
            if incremental and not background:
                allowance = min(allowance, int(slot_remaining))
            if allowance < 30:
                if not background:
                    failures.append(brand + ': execution allowance reserved or exhausted')
                continue
            if background:
                progress['collection_attempt'] = now.isoformat()
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
            env['VOLVO_COLLECTION_SLICE_SECONDS'] = str(max(5, allowance - 40))
            output = ''
            try:
                command = [sys.executable, script, '--as-of',
                           now.isoformat() if background else f'{job_day}T{job_cutoff}:00']
                if background:
                    command += ['--collect-only']
                if schedule_start:
                    command += ['--since', schedule_start]
                result = subprocess.run(command, cwd=root / folder,
                                        env=env, timeout=allowance, stdout=subprocess.PIPE,
                                        stderr=subprocess.STDOUT, text=True)
                output = getattr(result, 'stdout', '') or ''
                if result.returncode:
                    if not background:
                        failures.append(brand + (': data collection pending' if result.returncode == 75 else ''))
                elif not background:
                    if use_backlog:
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
                elapsed = min(allowance, time.monotonic() - started)
                progress['used_seconds'] -= max(0, allowance - elapsed)
                if incremental:
                    if background:
                        progress['collection_seconds'] = progress.get('collection_seconds', 0) + elapsed
                    else:
                        progress['slot_seconds'][slot] = progress['slot_seconds'].get(slot, 0) + elapsed
                    if brand == 'volvo' and cache_file.exists():
                        save_cache(container, cache_file)
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
            for pdf in (root / folder / "output/pdf").iterdir() if (root / folder / "output/pdf").exists() else ():
                if not pdf.name.endswith(('.pdf', '.pdf.empty.json')):
                    continue
                name = f"pdf/{brand}/{pdf.name}"
                data = pdf.read_bytes()
                if existing_hashes.get(name) != hashlib.sha256(data).hexdigest():
                    container.upload_blob(name, data, overwrite=True)
            # Dispatch the completed brand immediately instead of waiting for the
            # other API. Receipts survive later generation failures/timeouts.
            with closing(sqlite3.connect(database)) as early_db:
                def early_checkpoint():
                    ensure_lease()
                    container.upload_blob('delivery.sqlite3', database.read_bytes(), overwrite=True)
                deliver(cfg, early_db, root, checkpoint=early_checkpoint, before_send=ensure_lease)
                early_checkpoint()
        with closing(sqlite3.connect(database)) as db:
            def checkpoint():
                ensure_lease()
                container.upload_blob("delivery.sqlite3", database.read_bytes(), overwrite=True)
            send_failures = deliver(cfg, db, root, checkpoint=checkpoint, before_send=ensure_lease)
            checkpoint()
            health = {}
            if incremental and cache_file.exists():
                with closing(sqlite3.connect(cache_file)) as cache_db:
                    metadata = {key: json.loads(value) for key, value in cache_db.execute('SELECT key,value FROM metadata')}
                    health = {'coverage': metadata.get('coverage'), 'pending_query': metadata.get('pending'),
                              'unassigned_vehicle_count': len(metadata.get('unassigned_vehicles', [])),
                              'inventory_checked': metadata.get('inventory_checked'),
                              'metadata_error': metadata.get('metadata_error')}
            summary = {"updated_at": datetime.now(IST).isoformat(),
                       "generation_failures": failures, "delivery_failures": send_failures,
                       "generation_seconds_today": progress['used_seconds'],
                       "queued_days": progress['backlog'],
                       "incremental_collection": incremental,
                       "collection_seconds_today": progress.get('collection_seconds', 0),
                       "volvo_cache": health,
                       "schedule_cutoffs_ist": CUTOFFS, "schedule_start": schedule_start,
                       "site_routes": cfg.get('routes', {}),
                       "receipts": dict(db.execute("SELECT brand,COUNT(*) FROM sent GROUP BY brand"))}
            container.upload_blob("status.json", json.dumps(summary), overwrite=True)
        LOG.info("Fleet cloud run complete: %s", summary)
        if incremental:
            notify_delays(cfg, progress, now, failures, ensure_lease, container)
        if failures or send_failures:
            raise RuntimeError("Some fleet reports failed; pending work will retry next timer run")
