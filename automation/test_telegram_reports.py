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
    def test_site_routes_never_fall_back_to_testing(self):
        cfg = {'routes': {'Dhoptala':'dh', 'Majri':'ma', 'Sasti':'sa', 'Gouri':'go',
                          'Dudhichua':'dc', 'Jayant':'ja'},
               'chats': {'scania':['testing'], 'volvo':['testing']}}
        for name, chat in [('Dhoptala','dh'),('Majri','ma'),('Sasti_Chandrapur','sa'),
                           ('Gouri','go'),('Dudhichua_East','dc'),('Dudhichua_West','dc'),
                           ('Dudhichua_Singrauli','dc'),('Jayant_East','ja'),('Jayant_West','ja')]:
            for brand in reports.PROJECTS:
                self.assertEqual(reports.recipients(cfg, brand, name+'.pdf'), [chat])
        self.assertEqual(reports.recipients(cfg, 'volvo', 'Unknown_sites.pdf'), [])

    def test_production_cutover_uses_cutoff_date_not_shift_start_date(self):
        cfg = {'routes': {'Sasti':'group'}, 'report_since':'2026-10-23T00:00:00+05:30'}
        for brand, name in [('scania','Sasti_Chandrapur_C3_20261022_0300.pdf'),
                            ('volvo','Sasti_ShiftC_2026-10-22_2100_0300.pdf')]:
            self.assertEqual(reports.recipients(cfg, brand, name), ['group'])
            self.assertEqual(reports.report_cutoff(brand, name).isoformat(), '2026-10-23T03:00:00+05:30')
        self.assertEqual(reports.recipients(cfg, 'volvo', 'Sasti_ShiftC_2026-10-22_2100_2300.pdf'), [])
        self.assertEqual(reports.recipients(cfg, 'scania', 'Sasti_Chandrapur_A1_20261022_1030.pdf'), [])

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

    def test_existing_installation_outbox_and_settle_delay(self):
        external = self.root / 'existing-installation'
        outbox = external / 'output/pdf'
        outbox.mkdir(parents=True)
        (outbox / 'existing.pdf').write_bytes(b'%PDF-existing')
        self.cfg['project_dirs'] = {'scania': str(external)}
        self.cfg['settle_seconds'] = 30
        with patch.object(reports, 'api') as send:
            reports.deliver(self.cfg, self.db, self.root)
            send.assert_not_called()
        self.cfg['settle_seconds'] = 0
        with patch.object(reports, 'api', return_value={'message_id': 1}) as send:
            reports.deliver(self.cfg, self.db, self.root)
            scania_files = [call.args[3].name for call in send.call_args_list if call.args[2]['caption'].startswith('Scania')]
            self.assertEqual(scania_files, ['existing.pdf', 'existing.pdf'])

    def test_multipart_upload_and_response(self):
        response = io.BytesIO(json.dumps({'ok': True, 'result': {'message_id': 9}}).encode())
        pdf = self.root / reports.PROJECTS['scania'][0] / 'output/pdf/report.pdf'
        with patch.object(reports.urllib.request, 'urlopen', return_value=response) as request:
            self.assertEqual(reports.api('secret', 'sendDocument', {'chat_id': '-1001'}, pdf)['message_id'], 9)
            body = request.call_args.args[0].data
            self.assertIn(b'name="document"', body)
            self.assertIn(b'%PDF-test', body)
            self.assertIn(b'-1001', body)

    def test_verification_uses_form_encoding(self):
        response = io.BytesIO(b'{"ok":true,"result":{"id":1}}')
        with patch.object(reports.urllib.request, 'urlopen', return_value=response) as request:
            reports.api('secret', 'getChat', {'chat_id': '-1001'})
            self.assertEqual(request.call_args.args[0].data, b'chat_id=-1001')
            self.assertEqual(request.call_args.args[0].get_header('Content-type'), 'application/x-www-form-urlencoded')

    def test_lost_cloud_lease_prevents_upload(self):
        def lost_lease():
            raise RuntimeError('lease lost')
        with patch.object(reports, 'api') as send:
            with self.assertRaisesRegex(RuntimeError, 'lease lost'):
                reports.deliver(self.cfg, self.db, self.root, before_send=lost_lease)
            send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
