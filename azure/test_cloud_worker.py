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

    def test_overlapping_invocation_does_not_generate_or_send(self):
        self.container.locked = True
        with patch.object(worker.subprocess, 'run') as generate, patch('automation.telegram_reports.api') as send:
            worker.run()
            generate.assert_not_called()
            send.assert_not_called()

    def test_daily_limit_defers_incomplete_previous_day_without_repeating_completed_fleet(self):
        self.container.files['progress.json'] = json.dumps({
            'day': '2026-10-21', 'used_seconds': 1800,
            'completed': {'scania': '18:45', 'volvo': '11:45'}}).encode()
        with patch.object(worker.subprocess, 'run', side_effect=self.generate) as generate, patch('automation.telegram_reports.api', return_value={'message_id': 1}):
            worker.run()
            commands = [call.args[0] for call in generate.call_args_list]
            self.assertEqual(commands[0][-1], '2026-10-22T11:45:00')
            self.assertEqual(commands[1][-1], '2026-10-21T18:45:00')
            progress = json.loads(self.container.files['progress.json'])
            self.assertEqual(progress['backlog']['volvo'], [])
            worker.run()
            commands = [call.args[0] for call in generate.call_args_list]
            self.assertEqual(commands[-1][-1], '2026-10-22T11:45:00')


if __name__ == '__main__':
    unittest.main()
