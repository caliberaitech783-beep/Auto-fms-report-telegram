"""Full scheduled pipeline with fake source records and real PDF rendering."""
from contextlib import ExitStack, closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import api_report_runner as runner
import volvo_api_v2 as volvo

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from automation.telegram_reports import deliver, recipients


class PipelineTests(unittest.TestCase):
    def test_six_cutoffs_new_vin_pdf_and_all_routes_without_duplicate_delivery(self):
        shifts = json.loads(runner.SHIFTS_FILE.read_text())
        routes = {'Dhoptala':'1', 'Majri':'2', 'Sasti':'3', 'Gouri':'4', 'Dudhichua':'5', 'Jayant':'6'}
        sites = [site for site in shifts['sites'] if any(key.lower() in site.lower() for key in routes)]
        self.assertEqual(len(sites), 9)
        config = {'sites': {site: {'vehicles': [{'vin': f'VIN{i}', 'name': f'V{i}'}]} for i, site in enumerate(sites)}}
        inventory = [{'vin': f'VIN{i}', 'customerVehicleName': f'V{i}'} for i in range(len(sites))]
        inventory.append({'vin': 'NEWVIN', 'customerVehicleName': 'Sasti V850'})
        requests = []
        epoch = volvo.parse_when('2026-10-27T00:00:00')

        def fetch(start, stop, auth, on_page):
            requests.append((start, stop))
            begin, end = volvo.parse_when(start), volvo.parse_when(stop)
            records = []
            current = begin.replace(minute=0, second=0)
            while current <= end:
                if current >= begin:
                    hours = (current - epoch).total_seconds()/3600
                    for vehicle in inventory:
                        records.append({'vin': vehicle['vin'], 'createdDateTime':volvo._utc_z(current),
                            'receivedDateTime':volvo._utc_z(current), 'hrTotalVehicleDistance':hours*20000,
                            'engineTotalFuelUsed':hours*10000, 'totalEngineHours':hours,
                            'accumulatedData': {'durationWheelbaseSpeedOverZero':hours*2400,
                                                'durationWheelbaseSpeedZero':hours*1200}})
                current += timedelta(minutes=30)
            on_page(records, stop, True)
            return records

        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            base = Path(folder)
            out = base/'output/pdf'
            for name, value in [('BASE',base), ('OUTBOX',out), ('QUEUE_FILE',out/'queue.jsonl')]:
                stack.enter_context(patch.object(runner,name,value))
            stack.enter_context(patch.dict('os.environ', {'VOLVO_INCREMENTAL_CACHE':'true',
                'VOLVO_AUTO_SITE_PREFIX':'true','TELEGRAM_ROUTES_JSON':json.dumps(routes)}))
            stack.enter_context(patch.object(runner,'sync_asset_groups',return_value=(config,None)))
            stack.enter_context(patch.object(volvo,'load_credentials',return_value=('fake','fake')))
            stack.enter_context(patch.object(volvo,'fetch_vehicle_inventory',return_value=inventory))
            stack.enter_context(patch.object(volvo,'fetch_driver_directory',return_value={}))
            stack.enter_context(patch.object(volvo,'fetch_statuses',side_effect=fetch))
            stack.enter_context(patch.object(runner,'load_config',return_value=None))
            for cutoff in runner.SCHEDULED_REPORT_TIMES:
                self.assertEqual(runner.main(f'2026-10-28T{cutoff}:00'),0)
            pdfs = list(out.glob('*.pdf'))
            self.assertEqual(len(pdfs),54)
            self.assertEqual(len(requests),6)
            # No later cutoff starts its download again at shift start.
            for previous, following in zip(requests, requests[1:]):
                self.assertEqual(volvo.parse_when(following[0]),volvo.parse_when(previous[1])-timedelta(minutes=10))
            # Rendered PDF contains the new, automatically enrolled vehicle.
            from pypdf import PdfReader
            text = '\n'.join(page.extract_text() for page in PdfReader(str(next(out.glob('Sasti*1130.pdf')))).pages)
            self.assertIn('Sasti V850',text)
            cfg = {'bot_token':'fake','routes':routes,'start_date':'2026-10-22'}
            project = base/'Volvo API Integration/api_v2_recovery/output/pdf'
            project.mkdir(parents=True)
            for pdf in pdfs:
                pdf.rename(project/pdf.name)
            with closing(sqlite3.connect(base/'receipts.sqlite3')) as db, patch('automation.telegram_reports.api', return_value={'message_id':123}) as send:
                self.assertEqual(deliver(cfg,db,base),0)
                self.assertEqual(send.call_count,54)
                self.assertEqual(deliver(cfg,db,base),0)
                self.assertEqual(send.call_count,54)
                for brand, filename, chat in db.execute('SELECT brand,file,chat FROM sent'):
                    self.assertIn(chat,recipients(cfg,brand,filename))
                self.assertEqual({r[0] for r in db.execute('SELECT chat FROM sent')},set(routes.values()))


if __name__ == '__main__':
    unittest.main()


