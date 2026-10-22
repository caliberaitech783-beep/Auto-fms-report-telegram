import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import telegram_reports as reports


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.cfg = {'bot_token': 'secret', 'chats': {'scania': ['one', 'two'], 'volvo': ['one']}}
        for folder, _ in reports.PROJECTS.values():
            out = self.root / folder / 'output/pdf'
            out.mkdir(parents=True)
            (out / 'report.pdf').write_bytes(b'%PDF-test')

    def test_successful_recipients_are_not_repeated_and_failure_retries(self):
        with patch.object(reports, 'api', side_effect=[{'message_id': 1}, reports.TelegramError('offline'), {'message_id': 2}]) as send:
            self.assertEqual(reports.deliver(self.cfg, self.db, self.root), 1)
            self.assertEqual(send.call_count, 3)
        with patch.object(reports, 'api', return_value={'message_id': 3}) as send:
            self.assertEqual(reports.deliver(self.cfg, self.db, self.root), 0)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(send.call_args.args[2]['chat_id'], 'two')
        with patch.object(reports, 'api') as send:
            self.assertEqual(reports.deliver(self.cfg, self.db, self.root), 0)
            send.assert_not_called()

    def test_changed_report_and_new_destination_are_delivered(self):
        with patch.object(reports, 'api', return_value={'message_id': 1}):
            reports.deliver(self.cfg, self.db, self.root)
        pdf = self.root / reports.PROJECTS['scania'][0] / 'output/pdf/report.pdf'
        pdf.write_bytes(b'%PDF-corrected')
        with patch.object(reports, 'api', return_value={'message_id': 2}) as send:
            reports.deliver(self.cfg, self.db, self.root)
            self.assertEqual(send.call_count, 2)

    def test_previous_files_excluded_by_explicit_start_date(self):
        self.cfg['start_date'] = '2099-01-01'
        with patch.object(reports, 'api') as send:
            reports.deliver(self.cfg, self.db, self.root)
            send.assert_not_called()

    def test_activation_date_persists_for_later_retries(self):
        with patch.object(reports, 'api', return_value={'message_id': 1}):
            reports.deliver(self.cfg, self.db, self.root)
        saved = self.db.execute("SELECT value FROM settings WHERE key='start_date'").fetchone()[0]
        self.assertTrue(saved)
        with patch.object(reports, 'api'):
            reports.deliver(self.cfg, self.db, self.root)
        self.assertEqual(self.db.execute("SELECT value FROM settings WHERE key='start_date'").fetchone()[0], saved)

    def test_http_errors_do_not_expose_token(self):
        error = urllib.error.HTTPError('https://api.telegram.org/botSECRET/sendDocument', 429, 'limited', {}, None)
        with patch.object(reports.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaises(reports.TelegramError) as caught:
                reports.api('SECRET', 'sendDocument', {})
        self.assertNotIn('SECRET', str(caught.exception))
        self.assertIn('429', str(caught.exception))

    def test_multipart_upload_and_response(self):
        response = io.BytesIO(json.dumps({'ok': True, 'result': {'message_id': 9}}).encode())
        pdf = self.root / reports.PROJECTS['scania'][0] / 'output/pdf/report.pdf'
        with patch.object(reports.urllib.request, 'urlopen', return_value=response) as request:
            self.assertEqual(reports.api('secret', 'sendDocument', {'chat_id': '-1001'}, pdf)['message_id'], 9)
            body = request.call_args.args[0].data
            self.assertIn(b'name="document"', body)
            self.assertIn(b'%PDF-test', body)
            self.assertIn(b'-1001', body)


if __name__ == '__main__':
    unittest.main()
