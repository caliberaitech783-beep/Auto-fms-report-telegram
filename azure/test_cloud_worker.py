"""Offline lifecycle tests with an in-memory Blob service and fake fleet APIs."""
from datetime import datetime, timezone, timedelta
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

# Exercise worker logic independently of the Azure SDK/network.
class Missing(Exception):
    pass

class Exists(Exception):
    pass

class HttpError(Exception):
    status_code = 409

exceptions = types.ModuleType('azure.core.exceptions')
exceptions.ResourceNotFoundError = Missing
exceptions.ResourceExistsError = Exists
exceptions.HttpResponseError = HttpError
blob_sdk = types.ModuleType('azure.storage.blob')
blob_sdk.BlobServiceClient = types.SimpleNamespace(from_connection_string=None)
sys.modules.setdefault('azure.core.exceptions', exceptions)
sys.modules.setdefault('azure.storage.blob', blob_sdk)
spec = importlib.util.spec_from_file_location('cloud_worker', Path(__file__).with_name('cloud_worker.py'))
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class Container:
    def __init__(self):
        self.files = {}
        self.locked = False
    def get_blob_client(self, name):
        container = self
        class Lock:
            def upload_blob(self, *args, **kwargs):
                pass
            def acquire_lease(self, **kwargs):
                if container.locked:
                    raise HttpError()
                container.locked = True
                return self
            def renew(self):
                pass
            def release(self):
                container.locked = False
        return Lock()
    def create_container(self):
        pass
    def download_blob(self, name):
        if name not in self.files:
            raise Missing()
        return types.SimpleNamespace(readall=lambda: self.files[name])
    def upload_blob(self, name, data, overwrite=False):
        self.files[name] = data.encode() if isinstance(data, str) else data
    def list_blobs(self, name_starts_with):
        return [types.SimpleNamespace(name=name) for name in self.files if name.startswith(name_starts_with)]


class CloudLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for folder, script in worker.PROJECTS.values():
            project = self.root / folder
            project.mkdir(parents=True)
            (project / script).write_text('# test fixture')
        self.container = Container()
        self.env = {'REPORTS_ENABLED': 'true', 'AzureWebJobsStorage': 'fake',
                    'TELEGRAM_BOT_TOKEN': 'fake', 'TELEGRAM_CHAT_ID': '-1001',
                    'REPORT_START_DATE': '2026-10-22'}
        for brand in worker.PROJECTS:
            self.env[brand.upper() + '_CONFIG_JSON'] = json.dumps({'client_id': 'fixture', 'client_secret': 'fixture'})
            self.env[brand.upper() + '_SITES_JSON'] = json.dumps({'sites': {'Test': {}}})
        fixed = datetime(2026, 10, 22, 12, 0, tzinfo=worker.IST)
        clock = types.SimpleNamespace(now=lambda tz: fixed, fromisoformat=datetime.fromisoformat)
        service = types.SimpleNamespace(get_container_client=lambda name: self.container)
        for context in (patch.dict(worker.os.environ, self.env), patch.object(worker, 'SOURCE', self.root),
                        patch.object(worker.time, 'tzset', create=True), patch.object(worker, 'datetime', clock),
                        patch.object(worker.BlobServiceClient, 'from_connection_string', return_value=service)):
            context.start()
            self.addCleanup(context.stop)

    def generate(self, args, cwd, **kwargs):
        out = cwd / 'output/pdf'
        out.mkdir(parents=True, exist_ok=True)
        (out / 'test_20261022.pdf').write_bytes(b'%PDF-fixture')
        return types.SimpleNamespace(returncode=0)

    def test_restart_restores_pdfs_and_receipts_without_resending(self):
        with patch.object(worker.subprocess, 'run', side_effect=self.generate) as generate, patch('automation.telegram_reports.api', return_value={'message_id': 1}) as send:
            worker.run()
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(send.call_count, 2)
            self.assertIn('delivery.sqlite3', self.container.files)
            worker.run()
            self.assertEqual(generate.call_count, 2)
            self.assertEqual(send.call_count, 2)

    def test_generation_failure_does_not_block_other_fleet(self):
        calls = []
        def generate(args, cwd, **kwargs):
            calls.append(cwd)
            if 'Scania' in str(cwd):
                return types.SimpleNamespace(returncode=1)
            return self.generate(args, cwd, **kwargs)
        with patch.object(worker.subprocess, 'run', side_effect=generate), patch('automation.telegram_reports.api', return_value={'message_id': 1}) as send:
            with self.assertRaises(RuntimeError):
                worker.run()
            self.assertEqual(len(calls), 2)
            self.assertEqual(send.call_count, 1)
            self.assertIn('Volvo', send.call_args.args[2]['caption'])

    def test_exhausted_daily_budget_prevents_fleet_api_execution(self):
        self.container.files['progress.json'] = json.dumps({'day': '2026-10-22', 'used_seconds': 1800, 'completed': {}}).encode()
        with patch.object(worker.subprocess, 'run') as generate:
            with self.assertRaises(RuntimeError):
                worker.run()
            generate.assert_not_called()

    def test_disabled_worker_does_not_access_storage(self):
        with patch.dict(worker.os.environ, {'REPORTS_ENABLED': 'false'}), patch.object(worker.BlobServiceClient, 'from_connection_string') as service:
            worker.run()
            service.assert_not_called()

    def test_empty_report_markers_survive_worker_restart(self):
        filename = 'Sasti_ShiftC_2026-10-21_2100_0300.pdf.empty.json'
        def generate(args, cwd, **kwargs):
            result = self.generate(args,cwd,**kwargs)
            (cwd/'output/pdf'/filename).write_text('{"reason":"no qualifying vehicles"}')
            return result
        with patch.object(worker.subprocess,'run',side_effect=generate), patch('automation.telegram_reports.api',return_value={'message_id':1}):
            worker.run()
        self.assertIn('pdf/volvo/'+filename,self.container.files)
        def inspect(cfg,db,root,**kwargs):
            for folder,_ in worker.PROJECTS.values():
                self.assertTrue((root/folder/'output/pdf'/filename).exists())
            return 0
        with patch.object(worker,'deliver',side_effect=inspect), patch.object(worker.subprocess,'run') as generate:
            worker.run()
            generate.assert_not_called()

    def test_azure_utc_setting_is_normalized_to_ist_for_fleet_scripts(self):
        with patch.dict(worker.os.environ, {'REPORT_SCHEDULE_START':'2026-10-22T10:39:58+00:00'}):
            cfg = worker.prepare(self.root/'prepared')
        self.assertEqual(cfg['report_since'], '2026-10-22T16:09:58+05:30')

    def test_schedule_cutover_keeps_daily_allowance_and_clears_testing_backlog(self):
        self.container.files['progress.json'] = json.dumps({
            'day':'2026-10-22', 'used_seconds':1800, 'completed':{'scania':'18:45'},
            'backlog':{'scania':['2026-10-21'], 'volvo':['2026-10-21']}}).encode()
        with patch.dict(worker.os.environ, {'REPORT_SCHEDULE_START':'2026-10-22T12:01:00+05:30'}), \
             patch.object(worker.subprocess, 'run') as generate:
            with self.assertRaises(RuntimeError):
                worker.run()
            generate.assert_not_called()
        progress = json.loads(self.container.files['progress.json'])
        self.assertEqual(progress['used_seconds'], 1800)
        self.assertEqual(progress['completed'], {})
        self.assertEqual(progress['backlog'], {'scania':[], 'volvo':[]})

    def test_three_am_pdf_is_restored_by_cutoff_day_despite_previous_shift_date(self):
        fixed = datetime(2026,10,23,9,tzinfo=worker.IST)
        clock = types.SimpleNamespace(now=lambda tz:fixed, fromisoformat=datetime.fromisoformat)
        filename = 'Sasti_ShiftC_2026-10-22_2100_0300.pdf'
        self.container.files['pdf/volvo/'+filename] = b'%PDF-night'
        restored = []
        def generate(args, cwd, **kwargs):
            if 'Volvo' in str(cwd):
                restored.append((cwd/'output/pdf'/filename).exists())
            return types.SimpleNamespace(returncode=0)
        with patch.object(worker,'datetime',clock), patch.object(worker.subprocess,'run',side_effect=generate), \
             patch('automation.telegram_reports.api',return_value={'message_id':1}):
            worker.run()
        self.assertEqual(restored,[True])

    def test_overlapping_invocation_does_not_generate_or_send(self):
        self.container.locked = True
        with patch.object(worker.subprocess, 'run') as generate, patch('automation.telegram_reports.api') as send:
            worker.run()
            generate.assert_not_called()
            send.assert_not_called()

    def test_later_cutoffs_keep_runtime_even_after_early_failures(self):
        with patch.dict(worker.os.environ, {'VOLVO_INCREMENTAL_CACHE':'true', 'MAX_GENERATION_SECONDS_PER_DAY':'3600'}):
            early = datetime(2026,10,27,9,tzinfo=worker.IST)
            progress = {'used_seconds': 2640}
            # Four future cutoffs retain 4 * 240 seconds.
            self.assertEqual(worker.generation_allowance(progress, 'volvo', early), 0)
            later = early.replace(hour=18)
            self.assertEqual(worker.generation_allowance(progress, 'volvo', later), 300)
            progress['used_seconds'] = 3600
            self.assertEqual(worker.generation_allowance(progress, 'volvo', later), 0)

    def test_background_collection_cannot_consume_report_reserves(self):
        with patch.dict(worker.os.environ, {'VOLVO_INCREMENTAL_CACHE':'true', 'MAX_GENERATION_SECONDS_PER_DAY':'3600'}):
            now = datetime(2026,10,27,0,tzinfo=worker.IST)
            self.assertEqual(worker.generation_allowance({'used_seconds':0}, 'volvo', now, True), 90)
            self.assertEqual(worker.generation_allowance({'used_seconds':0,'collection_seconds':1800}, 'volvo', now, True), 0)
            self.assertEqual(worker.generation_allowance({'used_seconds':2000}, 'volvo', now, True), 0)

    def test_incremental_failure_does_not_reopen_exhausted_slot(self):
        self.container.files['progress.json'] = json.dumps({'day':'2026-10-22', 'used_seconds':600,
            'completed':{'scania':'11:30'},'slot_seconds':{'volvo:2026-10-22:11:30':480}}).encode()
        with patch.dict(worker.os.environ, {'VOLVO_INCREMENTAL_CACHE':'true', 'MAX_GENERATION_SECONDS_PER_DAY':'3600'}), \
             patch.object(worker.subprocess,'run') as generate, patch('cloud_worker.api', return_value={'message_id':2}):
            with self.assertRaises(RuntimeError):
                worker.run()
            generate.assert_not_called()
        self.assertEqual(json.loads(self.container.files['progress.json'])['used_seconds'],600)

    def test_cache_survives_a_timed_out_generation_and_restart(self):
        import gzip
        def generate(args, cwd, **kwargs):
            if 'Volvo' not in str(cwd):
                return self.generate(args,cwd,**kwargs)
            state = cwd/'state/fleet.sqlite3'
            state.parent.mkdir()
            with sqlite3.connect(state) as db:
                db.execute('CREATE TABLE metadata (key TEXT PRIMARY KEY,value TEXT)')
                db.execute('INSERT INTO metadata VALUES (?,?)', ('pending',json.dumps({'cursor':'saved-page'})))
                db.commit()
            db.close()
            raise worker.subprocess.TimeoutExpired(args,30)
        with patch.dict(worker.os.environ, {'VOLVO_INCREMENTAL_CACHE':'true','MAX_GENERATION_SECONDS_PER_DAY':'3600'}), \
             patch.object(worker.subprocess,'run',side_effect=generate), \
             patch('automation.telegram_reports.api',return_value={'message_id':1}):
            with self.assertRaises(RuntimeError):
                worker.run()
        archive = self.container.files['cache/volvo/fleet.sqlite3.gz']
        restored = self.root/'restored.sqlite3'
        restored.write_bytes(gzip.decompress(archive))
        with sqlite3.connect(restored) as db:
            self.assertEqual(json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])['cursor'],'saved-page')
        db.close()

    def test_delay_notice_is_sent_once_to_affected_groups(self):
        cfg = {'bot_token':'fake','routes':{'Sasti':'s','Dudhichua':'d','Majri':'m'}}
        progress = {'day':'2026-10-22','completed':{'scania':'11:30'}}
        now = datetime(2026,10,22,11,45,tzinfo=worker.IST)
        with patch.object(worker,'api',return_value={'message_id':1}) as send:
            worker.notify_delays(cfg,progress,now,['volvo'],lambda:None,self.container)
            self.assertEqual(send.call_count,3)
            worker.notify_delays(cfg,progress,now,['volvo'],lambda:None,self.container)
            self.assertEqual(send.call_count,3)

    def test_scania_delivery_happens_before_volvo_generation(self):
        order = []
        def generate(args,cwd,**kwargs):
            order.append('scania_generate' if 'Scania' in str(cwd) else 'volvo_generate')
            return self.generate(args,cwd,**kwargs)
        def send(*args,**kwargs):
            order.append('send')
            return {'message_id':1}
        with patch.object(worker.subprocess,'run',side_effect=generate), patch('automation.telegram_reports.api',side_effect=send):
            worker.run()
        self.assertLess(order.index('send'),order.index('volvo_generate'))

    def test_daily_limit_defers_incomplete_previous_day_without_repeating_completed_fleet(self):
        self.container.files['progress.json'] = json.dumps({
            'day': '2026-10-21', 'used_seconds': 1800,
            'completed': {'scania': '23:00', 'volvo': '11:30'}}).encode()
        with patch.object(worker.subprocess, 'run', side_effect=self.generate) as generate, patch('automation.telegram_reports.api', return_value={'message_id': 1}):
            worker.run()
            commands = [call.args[0] for call in generate.call_args_list]
            self.assertEqual(commands[0][-1], '2026-10-22T11:30:00')
            self.assertEqual(commands[1][-1], '2026-10-21T23:00:00')
            progress = json.loads(self.container.files['progress.json'])
            self.assertEqual(progress['backlog']['volvo'], [])
            worker.run()
            commands = [call.args[0] for call in generate.call_args_list]
            self.assertEqual(commands[-1][-1], '2026-10-22T11:30:00')


if __name__ == '__main__':
    unittest.main()
